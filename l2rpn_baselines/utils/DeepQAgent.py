# Copyright (c) 2020, RTE (https://www.rte-france.com)
# See AUTHORS.txt
# This Source Code Form is subject to the terms of the Mozilla Public License, version 2.0.
# If a copy of the Mozilla Public License, version 2.0 was not distributed with this file,
# you can obtain one at http://mozilla.org/MPL/2.0/.
# SPDX-License-Identifier: MPL-2.0
# This file is part of L2RPN Baselines, L2RPN Baselines a repository to host baselines for l2rpn competitions.

import os
import numpy as np
from abc import abstractmethod, ABC
from tqdm import tqdm
import tensorflow as tf

from grid2op.Agent import AgentWithConverter
from grid2op.Converter import IdToAct

from l2rpn_baselines.utils.ReplayBuffer import ReplayBuffer
from l2rpn_baselines.utils.TrainingParam import TrainingParam
import pdb


class DeepQAgent(AgentWithConverter):
    def __init__(self,
                 action_space,
                 name="DeepQAgent",
                 lr=1e-5):
        AgentWithConverter.__init__(self, action_space, action_space_converter=IdToAct)

        # and now back to the origin implementation
        self.replay_buffer = None

        self.deep_q = None
        self.lr = lr
        self.training_param = None
        self.process_buffer = []
        self.tf_writer = None
        self.name = name
        self.losses = None

    @abstractmethod
    def init_deep_q(self, transformed_observation):
        pass

    # grid2op.Agent interface
    def convert_obs(self, observation):
        return np.concatenate((observation.rho, observation.line_status, observation.topo_vect))

    def my_act(self, transformed_observation, reward, done=False):
        if self.deep_q is None:
            self.init_deep_q(transformed_observation)
        predict_movement_int, *_ = self.deep_q.predict_movement(transformed_observation.reshape(1, -1), epsilon=0.0)
        return int(predict_movement_int)

    # baseline interface
    def load(self, path):
        # not modified compare to original implementation
        self.deep_q.load_network(path)

    def save(self, path):
        if path is not None:
            self.deep_q.save_network(os.path.join(path, self.name))

    def set_chunk(self, env, nb):
        env.set_chunk_size(int(max(100, nb)))

    def train(self,
              env,
              iterations,
              save_path,
              logdir,
              training_param=TrainingParam()):
        self.training_param = training_param
        self._init_replay_buffer()

        # Create file system related vars
        if save_path is not None:
            save_path = os.path.abspath(save_path)
            os.makedirs(save_path, exist_ok=True)

        if logdir is not None:
            logpath = os.path.join(logdir, self.name)
            self.tf_writer = tf.summary.create_file_writer(logpath, name=self.name)
        else:
            logpath = None
            self.tf_writer = None
        UPDATE_FREQ = 100  # update tensorboard every "UPDATE_FREQ" steps
        SAVING_NUM = 1000

        # same as in the original implemenation, except the process buffer is now in this class
        observation_num = 0

        # some parameters have been move to a class named "training_param" for convenience
        epsilon = training_param.INITIAL_EPSILON

        # now the number of alive frames and total reward depends on the "underlying environment". It is vector instead
        # of scalar
        alive_frame, total_reward = self._init_global_train_loop()
        reward, done = self._init_local_train_loop()
        epoch_num = 0
        self.losses = np.zeros(iterations)
        alive_frames = np.zeros(iterations)
        total_rewards = np.zeros(iterations)
        with tqdm(total=iterations) as pbar:
            while observation_num < iterations:
                if observation_num % 1000 == 999:
                    # for efficient reading of data: at early stage of training, it is advised to load
                    # data by chunk: the model will do game over pretty easily (no need to load all the dataset)
                    tmp = min(10000 * (iterations // observation_num), 10000)
                    self.set_chunk(env, int(max(10, tmp)))

                # reset or build the environment
                epoch_num = self._need_reset(env, observation_num, epoch_num, done)

                # Slowly decay the learning rate
                if epsilon > training_param.FINAL_EPSILON:
                    epsilon -= (training_param.INITIAL_EPSILON - training_param.FINAL_EPSILON) / training_param.EPSILON_DECAY

                initial_state = self._convert_process_buffer()
                if observation_num == 0:
                    # we initialize the NN with the proper shape
                    self.init_deep_q(initial_state)
                self._reset_process_buffer()

                # then we need to predict the next moves. Agents have been adapted to predict a batch of data
                pm_i, pq_v, act = self._next_move(initial_state, epsilon)

                reward, done = self._init_local_train_loop()
                for i in range(training_param.NUM_FRAMES):
                    temp_observation_obj, temp_reward, temp_done, _ = env.step(act)

                    # and then "de stack" the observations coming from different environments
                    self._update_process_buffer(temp_observation_obj)

                    done, reward, total_reward, alive_frame \
                        = self._update_loop(done, temp_reward, temp_done, alive_frame, total_reward, reward)

                self._store_new_state(initial_state, pm_i, reward, done)

                if self.replay_buffer.size() > training_param.MIN_OBSERVATION:
                    s_batch, a_batch, r_batch, d_batch, s2_batch = self.replay_buffer.sample(
                        training_param.MINIBATCH_SIZE)
                    loss = self.deep_q.train(s_batch, a_batch, r_batch, d_batch, s2_batch, observation_num)
                    self.deep_q.target_train()
                    self.losses[observation_num] = loss
                    if not np.all(np.isfinite(loss)):
                        # if the loss is not finite i stop the learning
                        print("ERROR INFINITE LOSS")
                        break

                # Save the network every 1000 iterations
                if observation_num % SAVING_NUM == 0 or observation_num == iterations - 1:
                    print("Saving Network")
                    self.save(save_path)

                # save some information to tensorboard
                if alive_frame:
                    alive_frames[epoch_num] = alive_frame
                    total_rewards[epoch_num] = total_reward
                self._save_tensorboard(observation_num, epoch_num, UPDATE_FREQ, total_rewards, alive_frames)
                observation_num += 1
                pbar.update(1)

    # auxiliary functions
    def _convert_all_act(self, act_as_integer):
        res = []
        for act_id in act_as_integer:
            res.append(self.convert_act(act_id))
        return res

    def _need_reset(self, env, observation_num, epoch_num, done):
        if done or observation_num == 0:
            self._reset_process_buffer()
            obs = env.reset()
            tmp_obs = self.convert_obs(obs)
            self.process_buffer.append(tmp_obs)
            epoch_num += 1
            if epoch_num % len(env.chronics_handler.real_data.subpaths) == 0:
                # re shuffle the data
                env.chronics_handler.shuffle(lambda x: x[np.random.choice(len(x), size=len(x), replace=False)])
        return epoch_num

    def _reset_process_buffer(self):
        self.process_buffer = []

    def _init_replay_buffer(self):
        self.replay_buffer = ReplayBuffer(self.training_param.BUFFER_SIZE)

    def _convert_process_buffer(self):
        """Converts the list of NUM_FRAMES images in the process buffer
        into one training sample"""
        # here i simply concatenate the action in case of multiple action in the "buffer"
        if self.training_param.NUM_FRAMES != 1:
            raise RuntimeError("This h_need_resetas not been tested with self.training_param.NUM_FRAMES != 1 for now")
        # return np.array([np.concatenate(el) for el in self.process_buffer])
        return np.concatenate(self.process_buffer).reshape(1, -1)

    def _update_process_buffer(self, temp_observation_obj):
        self.process_buffer.append(self.convert_obs(temp_observation_obj))
        # for worker_id, obs in enumerate(temp_observation_obj):
        #     self.process_buffer[worker_id].append(self.convert_obs(temp_observation_obj[worker_id]))

    def _store_new_state(self, initial_state, predict_movement_int, reward, done):
        # vectorized version of the previous code
        new_state = self._convert_process_buffer()
        self.replay_buffer.add(initial_state.reshape(-1),
                               predict_movement_int.reshape(-1),
                               reward,
                               done,
                               new_state.reshape(-1))

        # # same as before, but looping through the "underlying environment"
        # for sub_env_id in range(self.nb_process):
        #     self.replay_buffer.add(initial_state[sub_env_id],
        #                                  predict_movement_int[sub_env_id],
        #                                  reward[sub_env_id],
        #                                  done[sub_env_id],
        #                                  new_state[sub_env_id])

    def _next_move(self, curr_state, epsilon):
        pm_i, pq_v = self.deep_q.predict_movement(curr_state, epsilon)
        act = self._convert_all_act(pm_i)
        if len(act) == 1:
            act = act[0]
        # act = self.convert_act(pm_i)
        # # and build the convenient vectors (it was scalars before)
        # predict_movement_int = []
        # predict_q_value = []
        # acts = []
        # for p_id in range(self.nb_process):
        #     predict_movement_int.append(pm_i[p_id])
        #     predict_q_value.append(pq_v[p_id])
        #     # and then we convert it to a valid action
        #     acts.append(self.convert_act(pm_i[p_id]))
        return pm_i, pq_v, act

    def _init_global_train_loop(self):
        # alive_frame = np.zeros(self.nb_process, dtype=np.int)
        # total_reward = np.zeros(self.nb_process, dtype=np.float)
        alive_frame = 0
        total_reward = 0.0
        return alive_frame, total_reward

    def _update_loop(self, done, temp_reward, temp_done, alive_frame, total_reward, reward):
        total_reward += temp_reward
        done = temp_done
        alive_frame += 1
        if done:
            alive_frame = 0
        return done, reward, total_reward, alive_frame
        # we need to handle vectors for "done"
        # reward[~temp_done] += temp_reward[~temp_done]
        #
        # done = done | temp_done
        #
        # # increase of 1 the number of frame alive for relevant "underlying environments"
        # alive_frame[~temp_done] += 1
        # # loop through the environment where a game over was done, and print the results
        # for env_done_idx in np.where(temp_done)[0]:
        #     print("For env with id {}".format(env_done_idx))
        #     print("\tLived with maximum time ", alive_frame[env_done_idx])
        #     print("\tEarned a total of reward equal to ", total_reward[env_done_idx])
        #
        # reward[temp_done] = 0.
        # total_reward[temp_done] = 0.
        # total_reward += reward
        # alive_frame[temp_done] = 0
        # return done, reward, total_reward, alive_frame

    def _init_local_train_loop(self):
        # reward, done = np.zeros(self.nb_process), np.full(self.nb_process, fill_value=False, dtype=np.bool)
        reward = 0.
        done = False
        return reward, done

    def _save_tensorboard(self, step, epoch_num, UPDATE_FREQ, epoch_rewards, epoch_alive):
        if self.tf_writer is None:
            return

        # Log some useful metrics every even updates
        if step % UPDATE_FREQ == 0:
            with self.tf_writer.as_default():
                mean_reward = np.mean(epoch_rewards[:epoch_num])
                mean_alive = np.mean(epoch_alive[:epoch_num])
                mean_reward_30 = mean_reward
                mean_alive_30 = mean_alive
                mean_reward_100 = mean_reward
                mean_alive_100 = mean_alive

                if epoch_num >= 100:
                    mean_reward_100 = np.mean(epoch_rewards[(epoch_num-100):epoch_num])
                    mean_alive_100 = np.mean(epoch_alive[(epoch_num-100):epoch_num])

                if epoch_num >= 30:
                    mean_reward_30 = np.mean(epoch_rewards[(epoch_num-30):epoch_num])
                    mean_alive_30 = np.mean(epoch_alive[(epoch_num-30):epoch_num])

                tf.summary.scalar("mean_reward", mean_reward, step)
                tf.summary.scalar("mean_alive", mean_alive, step)
                tf.summary.scalar("mean_reward_100", mean_reward_100, step)
                tf.summary.scalar("mean_alive_100", mean_alive_100, step)
                tf.summary.scalar("mean_reward_30", mean_reward_30, step)
                tf.summary.scalar("mean_alive_30", mean_alive_30, step)
                # tf.summary.scalar("lr", self.deep_q.train_lr, step)