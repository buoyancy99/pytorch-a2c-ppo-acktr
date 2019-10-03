import copy
import glob
import os
import time
import sys
from collections import deque

import gym
from gym import spaces
import assistive_gym
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F
import torch.optim as optim

from a2c_ppo_acktr import algo
from a2c_ppo_acktr.arguments import get_args
from a2c_ppo_acktr.envs import make_vec_envs
from a2c_ppo_acktr.model import Policy
from a2c_ppo_acktr.storage import RolloutStorage
from a2c_ppo_acktr.utils import get_vec_normalize, update_linear_schedule
from a2c_ppo_acktr.visualize import visdom_plot


args = get_args()

assert args.algo in ['a2c', 'ppo', 'acktr']
if args.recurrent_policy:
    assert args.algo in ['a2c', 'ppo'], \
        'Recurrent policy is not implemented for ACKTR'

action_space_robot = spaces.Box(low=np.array([-1.0]*args.action_robot), high=np.array([1.0]*args.action_robot), dtype=np.float32)
action_space_human = spaces.Box(low=np.array([-1.0]*args.action_human), high=np.array([1.0]*args.action_human), dtype=np.float32)

num_updates = int(args.num_env_steps) // args.num_steps // args.num_processes

torch.manual_seed(args.seed)
torch.cuda.manual_seed_all(args.seed)

if args.cuda and torch.cuda.is_available() and args.cuda_deterministic:
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True

try:
    os.makedirs(args.log_dir)
except (OSError, FileExistsError) as e:
    files = glob.glob(os.path.join(args.log_dir, '*.monitor.csv'))
    for f in files:
        os.remove(f)

eval_log_dir = args.log_dir + "_eval"

try:
    os.makedirs(eval_log_dir)
except OSError:
    files = glob.glob(os.path.join(eval_log_dir, '*.monitor.csv'))
    for f in files:
        os.remove(f)


def main():
    torch.set_num_threads(1)
    device = torch.device("cuda:0" if args.cuda else "cpu")

    if args.vis:
        from visdom import Visdom
        viz = Visdom(port=args.port)
        win = None

    envs = make_vec_envs(args.env_name, args.seed, args.num_processes,
                        args.gamma, args.log_dir, args.add_timestep, device, False)

    actor_critic_robot = Policy([args.obs_robot], action_space_robot,
        base_kwargs={'recurrent': args.recurrent_policy})
    actor_critic_robot.to(device)

    actor_critic_human = Policy([args.obs_human], action_space_human,
        base_kwargs={'recurrent': args.recurrent_policy})
    actor_critic_human.to(device)

    if args.algo == 'a2c':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, lr=args.lr,
                               eps=args.eps, alpha=args.alpha,
                               max_grad_norm=args.max_grad_norm)
    elif args.algo == 'ppo':
        agent_robot = algo.PPO(actor_critic_robot, args.clip_param, args.ppo_epoch, args.num_mini_batch,
                         args.value_loss_coef, args.entropy_coef, lr=args.lr,
                               eps=args.eps,
                               max_grad_norm=args.max_grad_norm)
        agent_human = algo.PPO(actor_critic_human, args.clip_param, args.ppo_epoch, args.num_mini_batch,
                         args.value_loss_coef, args.entropy_coef, lr=args.lr,
                               eps=args.eps,
                               max_grad_norm=args.max_grad_norm)
    elif args.algo == 'acktr':
        agent = algo.A2C_ACKTR(actor_critic, args.value_loss_coef,
                               args.entropy_coef, acktr=True)

    rollouts_robot = RolloutStorage(args.num_steps, args.num_processes,
                        [args.obs_robot], action_space_robot,
                        actor_critic_robot.recurrent_hidden_state_size)
    rollouts_human = RolloutStorage(args.num_steps, args.num_processes,
                        [args.obs_human], action_space_human,
                        actor_critic_human.recurrent_hidden_state_size)

    obs = envs.reset()
    obs_robot = obs[:, :args.obs_robot]
    obs_human = obs[:, args.obs_robot:]
    if len(obs_robot[0]) != args.obs_robot or len(obs_human[0]) != args.obs_human:
        print('robot obs shape:', obs_robot.shape, 'obs space robot shape:', [args.obs_robot])
        print('human obs shape:', obs_human.shape, 'obs space human shape:', [args.obs_human])
        exit()
    rollouts_robot.obs[0].copy_(obs_robot)
    rollouts_robot.to(device)
    rollouts_human.obs[0].copy_(obs_human)
    rollouts_human.to(device)

    episode_rewards = deque(maxlen=10)

    start = time.time()
    for j in range(num_updates):

        if args.use_linear_lr_decay:
            # decrease learning rate linearly
            if args.algo == "acktr":
                # use optimizer's learning rate since it's hard-coded in kfac.py
                update_linear_schedule(agent.optimizer, j, num_updates, agent.optimizer.lr)
            else:
                update_linear_schedule(agent_robot.optimizer, j, num_updates, args.lr)
                update_linear_schedule(agent_human.optimizer, j, num_updates, args.lr)

        if args.algo == 'ppo' and args.use_linear_clip_decay:
            agent_robot.clip_param = args.clip_param  * (1 - j / float(num_updates))
            agent_human.clip_param = args.clip_param  * (1 - j / float(num_updates))

        for step in range(args.num_steps):
            # Sample actions
            with torch.no_grad():
                value_robot, action_robot, action_log_prob_robot, recurrent_hidden_states_robot = actor_critic_robot.act(
                        rollouts_robot.obs[step],
                        rollouts_robot.recurrent_hidden_states[step],
                        rollouts_robot.masks[step])
                value_human, action_human, action_log_prob_human, recurrent_hidden_states_human = actor_critic_human.act(
                        rollouts_human.obs[step],
                        rollouts_human.recurrent_hidden_states[step],
                        rollouts_human.masks[step])

            # Obser reward and next obs
            action = torch.cat((action_robot, action_human), dim=-1)
            obs, reward, done, infos = envs.step(action)
            obs_robot = obs[:, :args.obs_robot]
            obs_human = obs[:, args.obs_robot:]

            for info in infos:
                if 'episode' in info.keys():
                    episode_rewards.append(info['episode']['r'])

            # If done then clean the history of observations.
            masks = torch.FloatTensor([[0.0] if done_ else [1.0]
                                       for done_ in done])
            rollouts_robot.insert(obs_robot, recurrent_hidden_states_robot, action_robot, action_log_prob_robot, value_robot, reward, masks)
            rollouts_human.insert(obs_human, recurrent_hidden_states_human, action_human, action_log_prob_human, value_human, reward, masks)

        with torch.no_grad():
            next_value_robot = actor_critic_robot.get_value(rollouts_robot.obs[-1],
                                                rollouts_robot.recurrent_hidden_states[-1],
                                                rollouts_robot.masks[-1]).detach()
            next_value_human = actor_critic_human.get_value(rollouts_human.obs[-1],
                                                rollouts_human.recurrent_hidden_states[-1],
                                                rollouts_human.masks[-1]).detach()

        rollouts_robot.compute_returns(next_value_robot, args.use_gae, args.gamma, args.tau)
        rollouts_human.compute_returns(next_value_human, args.use_gae, args.gamma, args.tau)

        value_loss_robot, action_loss_robot, dist_entropy_robot = agent_robot.update(rollouts_robot)
        value_loss_human, action_loss_human, dist_entropy_human = agent_human.update(rollouts_human)

        rollouts_robot.after_update()
        rollouts_human.after_update()

        # save for every interval-th episode or for the last epoch
        if (j % args.save_interval == 0 or j == num_updates - 1) and args.save_dir != "":
            save_path = os.path.join(args.save_dir, args.algo)
            try:
                os.makedirs(save_path)
            except OSError:
                pass

            # A really ugly way to save a model to CPU
            save_model_robot = actor_critic_robot
            save_model_human = actor_critic_human
            if args.cuda:
                save_model_robot = copy.deepcopy(actor_critic_robot).cpu()
                save_model_human = copy.deepcopy(actor_critic_human).cpu()

            save_model = [save_model_robot, save_model_human,
                          getattr(get_vec_normalize(envs), 'ob_rms', None)]

            torch.save(save_model, os.path.join(save_path, args.env_name + ".pt"))

        total_num_steps = (j + 1) * args.num_processes * args.num_steps

        if j % args.log_interval == 0 and len(episode_rewards) > 1:
            end = time.time()
            print("Robot/Human updates {}, num timesteps {}, FPS {} \n Last {} training episodes: mean/median reward {:.1f}/{:.1f}, min/max reward {:.1f}/{:.1f}\n".
                format(j, total_num_steps,
                       int(total_num_steps / (end - start)),
                       len(episode_rewards),
                       np.mean(episode_rewards),
                       np.median(episode_rewards),
                       np.min(episode_rewards),
                       np.max(episode_rewards), dist_entropy_robot,
                       value_loss_robot, action_loss_robot))
            sys.stdout.flush()

        if (args.eval_interval is not None
                and len(episode_rewards) > 1
                and j % args.eval_interval == 0):
            eval_envs = make_vec_envs(
                args.env_name, args.seed + args.num_processes, args.num_processes,
                args.gamma, eval_log_dir, args.add_timestep, device, True)

            vec_norm = get_vec_normalize(eval_envs)
            if vec_norm is not None:
                vec_norm.eval()
                vec_norm.ob_rms = get_vec_normalize(envs).ob_rms

            eval_episode_rewards = []

            obs = eval_envs.reset()
            obs_robot = obs[:, :args.obs_robot]
            obs_human = obs[:, args.obs_robot:]
            eval_recurrent_hidden_states_robot = torch.zeros(args.num_processes,
                            actor_critic_robot.recurrent_hidden_state_size, device=device)
            eval_recurrent_hidden_states_human = torch.zeros(args.num_processes,
                            actor_critic_human.recurrent_hidden_state_size, device=device)
            eval_masks = torch.zeros(args.num_processes, 1, device=device)

            while len(eval_episode_rewards) < 10:
                with torch.no_grad():
                    _, action_robot, _, eval_recurrent_hidden_states_robot = actor_critic_robot.act(
                        obs_robot, eval_recurrent_hidden_states_robot, eval_masks, deterministic=True)
                    _, action_human, _, eval_recurrent_hidden_states_human = actor_critic_human.act(
                        obs_human, eval_recurrent_hidden_states_human, eval_masks, deterministic=True)

                # Obser reward and next obs
                action = torch.cat((action_robot, action_human), dim=-1)
                obs, reward, done, infos = eval_envs.step(action)
                obs_robot = obs[:, :args.obs_robot]
                obs_human = obs[:, args.obs_robot:]

                eval_masks = torch.FloatTensor([[0.0] if done_ else [1.0]
                                                for done_ in done])
                for info in infos:
                    if 'episode' in info.keys():
                        eval_episode_rewards.append(info['episode']['r'])

            eval_envs.close()

            print(" Evaluation using {} episodes: mean reward {:.5f}\n".
                format(len(eval_episode_rewards),
                       np.mean(eval_episode_rewards)))
            sys.stdout.flush()

        if args.vis and j % args.vis_interval == 0:
            try:
                # Sometimes monitor doesn't properly flush the outputs
                win = visdom_plot(viz, win, args.log_dir, args.env_name,
                                  args.algo, args.num_env_steps)
            except IOError:
                pass


if __name__ == "__main__":
    main()
