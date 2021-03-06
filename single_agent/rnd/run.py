import time

import gym
import numpy as np
import tensorflow as tf

import snake_gym
from agent import Agent
from baselines.common.vec_env.subproc_vec_env import SubprocVecEnv
from buffer import Buffer
from utils.logx import EpochLogger
from utils.statistics_scalar import RunningMeanStd, RewardForwardFilter
from utils.wrappers import LogWrapper
from baselines.common.atari_wrappers import WarpFrame
from baselines.common.vec_env.vec_frame_stack import VecFrameStack


def create_env(n_env, seed, test=False):
    def make_env(rank):
        def _thunk():
            env = gym.make('Snake-rgb-v0')
            env.seed(seed + rank)
            env = LogWrapper(env)
            env = WarpFrame(env)
            return env
        return _thunk
    env = SubprocVecEnv([make_env(i) for i in range(n_env)])
    env = VecFrameStack(env, 2)
    return env


class Runner(object):

    def __init__(self,
                 epochs,
                 n_env,
                 seed,
                 gamma=0.99,
                 int_gamma=0.99,
                 lam=0.95,
                 train_epoch_len=128,
                 test_epoch_len=2000,
                 dtarg=0.01,
                 train_pi_iters=80,
                 train_v_iters=80,
                 logger_kwargs=dict()):

        self.epochs = epochs
        self.n_env = n_env
        self.train_epoch_len = train_epoch_len
        self.test_epoch_len = test_epoch_len
        self.dtarg = dtarg
        self.train_pi_iters = train_pi_iters
        self.train_v_iters = train_v_iters
        self.logger_kwargs = logger_kwargs

        self.checkpoints_dir = self.logger_kwargs['output_dir'] + '/checkpoints'
        
        tf.set_random_seed(seed)
        np.random.seed(seed)
        self.env = create_env(n_env, seed)

        self.obs = self.env.reset()

        self.obs_space = self.env.observation_space
        self.act_space = self.env.action_space

        self.obs_rms = RunningMeanStd(shape=(84, 84, 1))
        self.agent = Agent(self.obs_space, self.act_space)
        self.buffer = Buffer(gamma, lam)

    def _pre_normalize_obs(self):
        for step in range(int(10000 * self.n_env)):
            acts = np.random.randint(0, self.act_space.n, self.n_env)
            next_obs, _, _, _ = self.env.step(acts)
            self.obs_rms.update(next_obs[:, :, :, -1][:, :, :, None])
        self.obs = self.env.reset()
    
    def _collect_rollouts(self, logger):
        episode = 0
        for step in range(self.train_epoch_len):
            acts = self.agent.select_action(self.obs)
            ext_vals, int_vals = self.agent.get_val(self.obs)
            logger.store(ExtVal=ext_vals, IntVal=int_vals)
            next_obs, rews, dones, infos = self.env.step(acts)
            next_state = next_obs[:, :, :, -1][:, :, :, None]
            intrinsic_reward = self.agent.get_intrinsic_reward(((next_state - self.obs_rms.mean) / np.sqrt(self.obs_rms.var)).clip(-5, 5))
            self.buffer.store(self.obs, acts, rews, dones, ext_vals, int_vals, intrinsic_reward)
            # self.obs_rms.update(next_obs)
            self.obs = next_obs
            for info in infos:
                if info.get('ep_r'):
                    episode = 1
                    logger.store(EpRet=info.get('ep_r'))
                    logger.store(EpLen=info.get('ep_len'))
        if not episode:
            logger.store(EpRet=0, EpLen=0)
        last_ext_vals, last_int_vals = self.agent.get_val(self.obs)
        return last_ext_vals, last_int_vals

    def _run_train_phase(self, logger):
        start_time = time.time()
        last_ext_vals, last_int_vals = self._collect_rollouts(logger)
        obs_buf, act_buf, ext_ret_buf, int_ret_buf, adv_buf, state_buf = self.buffer.get(last_ext_vals, last_int_vals)
        norm_state_buf = ((state_buf - self.obs_rms.mean) / np.sqrt(self.obs_rms.var)).clip(-5, 5)
        self.obs_rms.update(state_buf)
        obs_buf /= 255.
        feed_dict = {
            self.agent.obs_ph: obs_buf,
            self.agent.state_ph: norm_state_buf,
            self.agent.act_ph: act_buf,
            self.agent.ext_ret_ph: ext_ret_buf,
            self.agent.int_ret_ph: int_ret_buf,
            self.agent.adv_ph: adv_buf,
        }

        for i in range(self.train_pi_iters):
            kl, entropy = self.agent.get_kl(feed_dict)
            logger.store(KL=kl, Entropy=entropy)
            if kl > 1.5 * self.dtarg:
                logger.log('Early stopping at step {} due to reaching max kl.'.format(i))
                break
            pi_loss = self.agent.update_pi_params(feed_dict)
            logger.store(PiLoss=pi_loss)
        for i in range(self.train_v_iters):
            ext_v_loss, int_v_loss = self.agent.update_v_params(feed_dict)
            logger.store(ExtVLoss=ext_v_loss, IntVLoss=int_v_loss)
            # rnd_loss = self.agent.update_rnd_params(feed_dict)
            # logger.store(ExtVLoss=ext_v_loss, IntVLoss=int_v_loss, RNDLoss=rnd_loss)
        # for i in range(5):
            rnd_loss = self.agent.update_rnd_params(feed_dict)
            logger.store(RNDLoss=rnd_loss)
        self.agent.sync_old_pi_params()
    
    def run_experiment(self):
        start_time = time.time()
        logger = EpochLogger(**self.logger_kwargs)
        self._pre_normalize_obs() #obs normalization is very important and greatly improve the performance.
        for epoch in range(1, self.epochs + 1):
            self._run_train_phase(logger)
            self.agent.save_model(self.checkpoints_dir, epoch)
            logger.log_tabular('Epoch', epoch)
            logger.log_tabular('EpRet', with_min_and_max=True)
            logger.log_tabular('EpLen', with_min_and_max=True)
            logger.log_tabular('ExtVal', average_only=True)
            logger.log_tabular('IntVal', average_only=True)
            logger.log_tabular('KL', average_only=True)
            logger.log_tabular('Entropy', average_only=True)
            logger.log_tabular('PiLoss', average_only=True)
            logger.log_tabular('ExtVLoss', average_only=True)
            logger.log_tabular('IntVLoss', average_only=True)
            logger.log_tabular('RNDLoss', average_only=True)
            logger.log_tabular('TotalInteractions', epoch * self.train_epoch_len * self.n_env)
            logger.log_tabular('Time', time.time() - start_time)
            logger.dump_tabular()

    def _run_test_phase(self, logger, render=True):
        env = create_env(1, 0)
        ep_r, ep_len = 0, 0
        obs = env.reset()
        for step in range(self.test_epoch_len):
            if render: env.render()
            act = self.agent.select_action(obs)
            next_obs, reward, done, info = env.step(act)
            # time.sleep(0.1)
            ep_r += reward
            ep_len += 1
            obs = next_obs
            
            if done:
                logger.store(TestEpRet=ep_r, TestEpLen=ep_len)

                obs = env.reset()
                ep_r, ep_len = 0, 0

    def run_test_and_render(self, model):
        logger = EpochLogger()
        self.agent.load_model(self.checkpoints_dir, model=model)
        for epoch in range(1, self.epochs + 1):
            self._run_test_phase(logger)


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument('--epochs', type=int, default=1000)
    parser.add_argument('--seed', '-s', type=int, default=0)
    parser.add_argument('--n_env', '-n', type=int, default=32)
    parser.add_argument('--exp_name', type=str, default='ppo')
    parser.add_argument('--test', action='store_true')
    parser.add_argument('--model', type=int, default=None)
    args = parser.parse_args()

    from utils.run_utils  import setup_logger_kwargs
    logger_kwargs = setup_logger_kwargs(args.exp_name, 'Snake-rgb-v0', args.seed)

    runner = Runner(args.epochs ,args.n_env, args.seed, logger_kwargs=logger_kwargs)
    if args.test:
        runner.run_test_and_render(args.model)
    else:
        runner.run_experiment()
