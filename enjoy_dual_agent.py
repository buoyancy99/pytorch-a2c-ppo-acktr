import argparse
import os

import numpy as np
import torch

from a2c_ppo_acktr.envs import VecPyTorch, make_vec_envs
from a2c_ppo_acktr.utils import get_render_func, get_vec_normalize


# workaround to unpickle olf model files
import sys
sys.path.append('a2c_ppo_acktr')

parser = argparse.ArgumentParser(description='RL')
parser.add_argument('--seed', type=int, default=1,
                    help='random seed (default: 1)')
parser.add_argument('--log-interval', type=int, default=10,
                    help='log interval, one log per n updates (default: 10)')
parser.add_argument('--env-name', default='ScratchItchJaco-v0',
                    help='environment to train on (default: ScratchItchJaco-v0)')
parser.add_argument('--load-dir', default='./trained_models/',
                    help='directory to save agent logs (default: ./trained_models/)')
parser.add_argument('--add-timestep', action='store_true', default=False,
                    help='add timestep to observations')
parser.add_argument('--non-det', action='store_true', default=False,
                    help='whether to use a non-deterministic policy')
parser.add_argument('--obs-robot', type=int, default=27,
                    help='Dimensionality of robot obs')
parser.add_argument('--obs-human', type=int, default=28,
                    help='Dimensionality of human obs')
args = parser.parse_args()

args.det = not args.non_det

env = make_vec_envs(args.env_name, args.seed + 1000, 1,
                            None, None, args.add_timestep, device='cpu',
                            allow_early_resets=False)

# Get a render function
render_func = get_render_func(env)

# We need to use the same statistics for normalization as used in training
actor_critic_robot, actor_critic_human, ob_rms = torch.load(os.path.join(args.load_dir, args.env_name + ".pt"))

vec_norm = get_vec_normalize(env)
if vec_norm is not None:
    vec_norm.eval()
    vec_norm.ob_rms = ob_rms

recurrent_hidden_states_robot = torch.zeros(1, actor_critic_robot.recurrent_hidden_state_size)
recurrent_hidden_states_human = torch.zeros(1, actor_critic_human.recurrent_hidden_state_size)
masks = torch.zeros(1, 1)

if render_func is not None:
    render_func('human')

obs = env.reset()
obs_robot = obs[:, :args.obs_robot]
obs_human = obs[:, args.obs_robot:]
if len(obs_robot[0]) != args.obs_robot or len(obs_human[0]) != args.obs_human:
    print('robot obs shape:', obs_robot.shape, 'obs space robot shape:', [args.obs_robot])
    print('human obs shape:', obs_human.shape, 'obs space human shape:', [args.obs_human])
    exit()

while True:
    with torch.no_grad():
        value_robot, action_robot, _, recurrent_hidden_states_robot = actor_critic_robot.act(
            obs_robot, recurrent_hidden_states_robot, masks, deterministic=args.det)
        value_human, action_human, _, recurrent_hidden_states_human = actor_critic_human.act(
            obs_human, recurrent_hidden_states_human, masks, deterministic=args.det)

    # Obser reward and next obs
    action = torch.cat((action_robot, action_human), dim=-1)
    obs, reward, done, _ = env.step(action)
    obs_robot = obs[:, :args.obs_robot]
    obs_human = obs[:, args.obs_robot:]

    masks.fill_(0.0 if done else 1.0)

    if render_func is not None:
        render_func('human')
