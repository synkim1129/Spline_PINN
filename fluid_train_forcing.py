from fluid_setups import Dataset
from spline_models import interpolate_states,superres_2d_velocity,superres_2d_pressure,get_Net
from operators import vector2HSV
from get_param import params,toCuda,toCpu,get_hyperparam_fluid
from Logger import Logger
import cv2
from torch.optim import Adam
import torch
import torch.nn.functional as F
import numpy as np

torch.manual_seed(0)
torch.set_num_threads(4)
np.random.seed(0)

mu = params.mu
rho = params.rho
dt = params.dt
ds = params.ds
params.width = 230 if params.width is None else params.width
params.height = 49 if params.height is None else params.height
resolution_factor = params.resolution_factor
orders_v = [params.orders_v,params.orders_v]
orders_p = [params.orders_p,params.orders_p]
v_size,p_size = np.prod([i+1 for i in orders_v]),np.prod([i+1 for i in orders_p])

# initialize dataset
dataset = Dataset(params.width,params.height,hidden_size=v_size+p_size,batch_size=params.batch_size,n_samples=params.n_samples,dataset_size=params.dataset_size,average_sequence_length=params.average_sequence_length,max_speed=params.max_speed,dt=params.dt,types=["DFG_benchmark","box","magnus","ecmo"], forcing=True, n_forcing=params.n_forcing)#"box","magnus","ecmo"

# initialize fluid model and optimizer
model = toCuda(get_Net(params, forcing=True))
optimizer = Adam(model.parameters(),lr=params.lr)

logger_pretrained = Logger(get_hyperparam_fluid(params), use_csv=False, use_tensorboard=False)
pretrained_model = toCuda(get_Net(params, forcing=False))
date_time, index = logger_pretrained.load_state(pretrained_model, None, datetime=params.load_pretrained_date_time, index=params.load_pretrained_index)
print(f"loaded pretrained model {params.net}: {date_time}, index: {index}")
pretrained_model.eval()

# initialize Logger and load model / optimizer if according parameters were given
logger = Logger(get_hyperparam_fluid(params) + ' with_forcing',use_csv=False,use_tensorboard=params.log)
logger.save_params_to_file(params)

if params.load_latest or params.load_date_time is not None or params.load_index is not None:
	load_logger = Logger(get_hyperparam_fluid(params) + ' with_forcing',use_csv=False,use_tensorboard=False)
	if params.load_optimizer:
		params.load_date_time, params.load_index = logger.load_state(model,optimizer,params.load_date_time,params.load_index)
	else:
		params.load_date_time, params.load_index = logger.load_state(model,None,params.load_date_time,params.load_index)
	params.load_index=int(params.load_index)
	print(f"loaded: {params.load_date_time}, {params.load_index}")
	if params.n_warmup_steps is not None: # optionally: warm up training pool after loading a pretrained model
		model.eval()
		for i in range(params.n_warmup_steps):
			v_cond,v_mask,old_hidden_state,_,_,_ = toCuda(dataset.ask())
			new_hidden_state = model(old_hidden_state,v_cond,v_mask)
			dataset.tell(toCpu(new_hidden_state))
			if i%(params.n_warmup_steps//100)==0:
				print(f"warmup {i/(params.n_warmup_steps//100)} %")
		model.train()
		
params.load_index = 0 if params.load_index is None else params.load_index

# diffusion operation (needed, if we want to put more loss-weight to regions close to the domain boundaries)
kernel_width = 2
kernel = torch.exp(-torch.arange(-2,2.001,4/(2*kernel_width)).float()**2)
kernel /= torch.sum(kernel)
kernel_x = toCuda(kernel.unsqueeze(0).unsqueeze(1).unsqueeze(3))
kernel_y = toCuda(kernel.unsqueeze(0).unsqueeze(1).unsqueeze(2))
def diffuse(T): # needed to put extra weight on domain borders
	T = F.conv2d(T,kernel_x,padding=[kernel_width,0])
	T = F.conv2d(T,kernel_y,padding=[0,kernel_width])
	return T

# training loop
for epoch in range(params.load_index,params.n_epochs):
	print(f"epochs: {epoch+1}/{params.n_epochs}")
 
	for i in range(params.n_batches_per_epoch):
		
		# retrieve batch from training pool
		v_cond,v_mask,old_hidden_state,offsets,sample_v_conds,sample_v_masks,v_obs,v_obs_mask = toCuda(dataset.ask())
		
		# predict new hidden state that contains the spline coeffients of the next timestep
		new_hidden_state = model(old_hidden_state,v_cond,v_mask,v_obs)
  
		with torch.no_grad():
			new_hidden_state_pretrained = pretrained_model(old_hidden_state,v_cond,v_mask)
		
		# compute physics informed loss
		loss_total = 0
		loss_domain_total = 0
		loss_boundary_total = 0
		loss_diff_total = 0
  
		v_obs = torch.zeros_like(v_obs)
	
		for j in range(params.n_samples):
			offset = torch.floor(offsets[j]*resolution_factor)/resolution_factor
			sample_v_cond = sample_v_conds[j]
			sample_v_mask = sample_v_masks[j]
			sample_domain_mask = 1-sample_v_mask
			# optionally: put additional border_weight on domain boundaries:
			sample_domain_mask = (sample_domain_mask + sample_domain_mask*diffuse(sample_v_mask)*params.border_weight).detach()
			
			# interpolate spline coeffients to obtain: a_z, v, dv_dt, grad_v, laplace_v, p, grad_p
			a_z,v,dv_dt,grad_v,laplace_v,p,grad_p = interpolate_states(old_hidden_state,new_hidden_state,offset,dt=dt,orders_v=orders_v,orders_p=orders_p,ds=ds)
			_, v_pred, _, _, _, _, _ = interpolate_states(old_hidden_state,new_hidden_state_pretrained,offset,dt=dt,orders_v=orders_v,orders_p=orders_p,ds=ds)
	
			
			# boundary loss
			loss_boundary = torch.mean(sample_v_mask[:,:,1:-1,1:-1]*(v-sample_v_cond[:,:,1:-1,1:-1])**2,dim=(1,2,3))
   
			# observation loss
			loss_diff = torch.mean(v_obs_mask[:,:,1:-1,1:-1]*(v-v_pred)**2,dim=(1,2,3))
			
			# navier stokes loss
			grad_v_x = grad_v[:,0:2]
			grad_v_y = grad_v[:,2:4]
			laplace_v_x = laplace_v[:,0:1]
			laplace_v_y = laplace_v[:,1:2]
			
			# -> residual loss
			loss_domain_x = torch.mean((sample_domain_mask[:,:,1:-1,1:-1])*(rho*(dv_dt[:,0:1]+v[:,0:1]*grad_v_x[:,0:1]+v[:,1:2]*grad_v_x[:,1:2])-mu*laplace_v_x+grad_p[:,0:1])**2,dim=(1,2,3))
			loss_domain_y = torch.mean((sample_domain_mask[:,:,1:-1,1:-1])*(rho*(dv_dt[:,1:2]+v[:,0:1]*grad_v_y[:,0:1]+v[:,1:2]*grad_v_y[:,1:2])-mu*laplace_v_y+grad_p[:,1:2])**2,dim=(1,2,3))
			loss_domain_res = loss_domain_x+loss_domain_y
			
			# # -> "upwind" loss (usually not needed)
			# target_v_x = v[:,0:1]+mu/rho*laplace_v_x-grad_p[:,0:1]/rho-v[:,0:1]*grad_v_x[:,0:1]-v[:,1:2]*grad_v_x[:,1:2]-dv_dt[:,0:1]
			# target_v_y = v[:,1:2]+mu/rho*laplace_v_y-grad_p[:,1:2]/rho-v[:,0:1]*grad_v_y[:,0:1]-v[:,1:2]*grad_v_y[:,1:2]-dv_dt[:,1:2]
			# target_v_x, target_v_y = target_v_x.detach(), target_v_y.detach()
			# loss_domain_up_x = torch.mean((sample_domain_mask[:,:,1:-1,1:-1])*(v[:,0:1]-target_v_x)**2,dim=(1,2,3))
			# loss_domain_up_y = torch.mean((sample_domain_mask[:,:,1:-1,1:-1])*(v[:,1:2]-target_v_y)**2,dim=(1,2,3))
			# loss_domain_up = loss_domain_up_x + loss_domain_up_y
			
			# # -> pressure loss (to put more weight on pressure field - usually not needed)
			# target_grad_p_x = mu*laplace_v_x-rho*(dv_dt[:,0:1]+v[:,0:1]*grad_v_x[:,0:1]+v[:,1:2]*grad_v_x[:,1:2])
			# target_grad_p_y = mu*laplace_v_y-rho*(dv_dt[:,1:2]+v[:,0:1]*grad_v_y[:,0:1]+v[:,1:2]*grad_v_y[:,1:2])
			# target_grad_p_x,target_grad_p_y = target_grad_p_x.detach(), target_grad_p_y.detach()
			# loss_grad_p_x = torch.mean((sample_domain_mask[:,:,1:-1,1:-1])*(grad_p[:,0:1]-target_grad_p_x)**2,dim=(1,2,3))
			# loss_grad_p_y = torch.mean((sample_domain_mask[:,:,1:-1,1:-1])*(grad_p[:,1:2]-target_grad_p_y)**2,dim=(1,2,3))
			# loss_domain_p = loss_grad_p_x+loss_grad_p_y
			
			# Accumulate losses directly
			loss_total += (params.loss_bound * loss_boundary + params.loss_domain_res * loss_domain_res + params.loss_diff * loss_diff) / params.n_samples
			loss_domain_total += torch.mean(loss_domain_res) / params.n_samples
			loss_boundary_total += torch.mean(loss_boundary) / params.n_samples
			loss_diff_total += torch.mean(loss_diff) / params.n_samples
   
			v_obs += v_obs_mask.expand(-1, 2, -1, -1) * F.pad(v_pred, (1, 1, 1, 1)) / params.n_samples

		
		# Compute final loss value for this batch
		loss_total = torch.mean(torch.log(loss_total))
  
		# Reset old gradients to 0 and compute new gradients with backpropagation
		model.zero_grad()
		loss_total.backward()
		
		# Clip gradients if necessary
		if params.clip_grad_value is not None:
			torch.nn.utils.clip_grad_value_(model.parameters(), params.clip_grad_value)
		
		if params.clip_grad_norm is not None:
			torch.nn.utils.clip_grad_norm_(model.parameters(), params.clip_grad_norm)
		
		# Optimize fluid model
		optimizer.step()
		
		# Update dataset with new hidden state
		dataset.tell(toCpu(new_hidden_state), v_obs=toCpu(v_obs))
		
		# Log training metrics
		print(f"iteration: {i}/{params.n_batches_per_epoch} loss_total: {loss_total.detach().cpu().numpy().item():.6f}, loss_domain: {loss_domain_total.detach().cpu().numpy().item():.6f}, loss_boundary: {loss_boundary_total.detach().cpu().numpy().item():.6f}, loss_diff: {loss_diff_total.detach().cpu().numpy().item():.6f}", end="\r")
		
		if i % 10 == 0:
			logger.log(f"loss_total", loss_total.detach().cpu().numpy(), epoch * params.n_batches_per_epoch + i)
			logger.log(f"loss_domain", loss_domain_total.detach().cpu().numpy(), epoch * params.n_batches_per_epoch + i)
			logger.log(f"loss_boundary", loss_boundary_total.detach().cpu().numpy(), epoch * params.n_batches_per_epoch + i)
			logger.log(f"loss_diff", loss_diff_total.detach().cpu().numpy(), epoch * params.n_batches_per_epoch + i)

	print(f"loss_total: {loss_total.detach().cpu().numpy().item()}, loss_domain: {loss_domain_total.detach().cpu().numpy().item()}, loss_boundary: {loss_boundary_total.detach().cpu().numpy().item()}, loss_diff: {loss_diff_total.detach().cpu().numpy().item()}")
 
	if params.log:
		logger.save_state(model,optimizer,epoch+1)