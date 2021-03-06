import sys
import itertools
from hfo import *

import numpy as np
import threading
import torch
from torch.autograd import Variable
from multiprocessing import Pool, Lock
from memory import ReplayMemory, Experience
import multiprocessing
from MADDPG import MADDPG
import numpy as np
import torch as th
import time
import h5py
import copy
import traceback
import subprocess

#LOGPATH = "/cs/ml/ddpgHFO/DDPG/"
LOGPATH = "/Users/surajnair/Documents/Tech/research/MADDPG_HFO/"
#LOGPATH = "/Users/anshulramachandran/Documents/Research/yisong/"
# LOGPATH = "/home/anshul/Desktop/"

LOGNUM = int(sys.argv[2])
EPDONE_LOAD = int(sys.argv[3])
PRIORITIZED = True

# Max training steps
MAX_EPISODES = 50000
# Max episode length
MAX_EP_STEPS = 500
# Base learning rate for the Actor network
ACTOR_LEARNING_RATE = .001
# Base learning rate for the Critic Network
CRITIC_LEARNING_RATE = .001
# Discount factor
GAMMA = 0.99
# Soft target update param
TAU = 0.001

EPS_ITERATIONS_ANNEAL = 200000

# Noise for exploration
EPS_GREEDY_INIT = 1.0
# EPS_ITERATIONS_ANNEAL = 1000000

# Size of replay buffer
capacity = 1000000
batch_size = 1024
eps_before_train = 5

GPUENABLED = False
ORACLE = False
PORT = int(sys.argv[1])

import time

FloatTensor = torch.cuda.FloatTensor if False else torch.FloatTensor


def connect():
    hfo = HFOEnvironment()
    hfo.connectToServer(LOW_LEVEL_FEATURE_SET,
                        'bin/teams/base/config/formations-dt', PORT,
                        'localhost', 'base_left', False)
    return hfo


def take_action_and_step(a, env, eps):
    if (np.random.random_sample() <= eps):
        acts = np.random.uniform(1, 10, 4)
        a[:4] = acts / np.sum(acts)
        a[4] = np.random.uniform(0, 100)
        a[5] = np.random.uniform(-180, 180)
        a[6] = np.random.uniform(-180, 180)
        a[7] = np.random.uniform(-180, 180)
        a[8] = np.random.uniform(0, 100)
        a[9] = np.random.uniform(-180, 180)

    index = np.argmax(a[:4])
    if index == 0:
        action = (DASH, a[4], a[5])
    elif index == 1:
        action = (TURN, a[6])
    elif index == 2:
        action = (TACKLE, a[7])
    else:
        action = (KICK, a[8], a[9])

    env.act(*action)
    terminal = env.step()
    s1 = env.getState()
    return s1, terminal


def get_curr_state_vars(s1):
    curr_ball_prox = s1[53]
    curr_kickable = s1[12]
    goal_proximity = s1[15]
    ball_dist = 1.0 - curr_ball_prox
    goal_dist = 1.0 - goal_proximity
    ball_ang_sin_rad = s1[51]
    ball_ang_cos_rad = s1[52]
    ball_ang_rad = np.arccos(ball_ang_cos_rad)
    if ball_ang_sin_rad < 0:
        ball_ang_rad *= -1.
    goal_ang_sin_rad = s1[13]
    goal_ang_cos_rad = s1[14]
    goal_ang_rad = np.arccos(goal_ang_cos_rad)
    if goal_ang_sin_rad < 0:
        goal_ang_rad *= -1.
    alpha = max(ball_ang_rad, goal_ang_rad) - min(ball_ang_rad, goal_ang_rad)
    # Law of Cosines
    curr_goal_dist = np.sqrt(ball_dist * ball_dist + goal_dist *
                             goal_dist - 2. * ball_dist * goal_dist * np.cos(alpha))

    return curr_ball_prox, curr_goal_dist, curr_kickable


def get_rewards(terminal, curr_ball_prox, curr_goal_dist, curr_kickable,
                old_ball_prox, old_goal_dist, old_kickable):

    r = 0.0

    # If game has finished, calculate reward based on whether or not a goal
    # was scored
    goal_made = False
    if terminal != IN_GAME:
        if int(terminal) == 1:
            goal_made = True
            r += 5
    else:
        # Movement to ball
        r += (curr_ball_prox - old_ball_prox)
        r += -3.0 * float(curr_goal_dist - old_goal_dist)

    return [r], goal_made


def run_process(maddpg, player_num, player_queue, root_queue, feedback_queue):
    env = connect()

    if player_num == 0:
        np.random.seed(12)
    else:
        np.random.seed(111)

    ITERATIONS = 0.0
    NUM_GOALS = 0.0
    for ep in xrange(MAX_EPISODES):
        ep_reward = 0.0
        ep_ave_max_q = 0.0

        status = IN_GAME
        states1 = env.getState()

        rr = np.zeros((1,))
        old_ball_proxs = np.zeros((1,))
        old_goal_dists = np.zeros((1,))
        old_kickables = np.zeros((1,))
        for j in xrange(MAX_EP_STEPS):
            # # Grab the state features from the environment
            states = states1
            try:
                states = torch.from_numpy(states).float()
            except:
                states = states.float()
            states = Variable(states).type(FloatTensor)
            actions = maddpg.select_action(states, player_num).data

            states1, terminal = take_action_and_step(
                actions.numpy(), env, max(0.1, 1 - ITERATIONS / EPS_ITERATIONS_ANNEAL))

            curr_ball_proxs, curr_goal_dists, curr_kickables = get_curr_state_vars(
                states1)
            action_rewards = np.zeros((1,))
            if j != 0:
                # print curr_ball_proxs,curr_goal_dists
                action_rewards, goal_made = get_rewards(terminal, curr_ball_proxs, curr_goal_dists,
                                                        curr_kickables, old_ball_proxs, old_goal_dists, old_kickables)

                if np.any(goal_made):
                    NUM_GOALS += 1

            rr += action_rewards
            old_ball_proxs = curr_ball_proxs
            old_goal_dists = curr_goal_dists
            old_kickables = curr_kickables

            states1 = np.stack(states1)
            states1 = torch.from_numpy(states1).float()

            # if j == MAX_EP_STEPS - 1:
            #     player_queue.put((states.data, actions, None, action_rewards, terminal, rr, ep))
            # else:
            #     player_queue.put((states.data, actions, states1, action_rewards, terminal, rr, ep))

            player_queue.put((states.data, actions, states1,
                              action_rewards, terminal, rr, (ep, j)))
            states = states1
            print("PLAYER", player_num, maddpg.episode_done)

            ITERATIONS += 1

            # EPISODE IS OVER
            ###########################################################
            if terminal:
                print(terminal)
                assert terminal != 5
                print('| Reward: ', rr, " | Episode", ep)
                break

            try:
                maddpg = root_queue.get(block=False)
            except:
                pass

            try:
                new = feedback_queue.get(timeout=1.5)
            except:
                print("TIMEOUT")


def run():
    n_agents = 2
    n_states = 77
    n_actions = 10

    q1 = multiprocessing.Queue()
    q2 = multiprocessing.Queue()
    r1 = multiprocessing.Queue()
    r2 = multiprocessing.Queue()
    fdbk1 = multiprocessing.Queue()
    fdbk2 = multiprocessing.Queue()

    maddpg = MADDPG(n_agents, n_states, n_actions,
                    batch_size, capacity, eps_before_train)

    maddpg.load(LOGPATH, LOGNUM, EPDONE_LOAD)

    p1 = multiprocessing.Process(
        target=run_process, args=(maddpg, 0, q1, r1, fdbk1))
    p2 = multiprocessing.Process(
        target=run_process, args=(maddpg, 1, q2, r2, fdbk2))

    print("Started")

    p1.start()
    time.sleep(5)
    p2.start()

    itr = 1
    if GPUENABLED:
        maddpg.to_gpu()

    try:

        while True:
            # State_t, Action, State_t+1, transition reward, terminal, episodre
            # reward, episode #
            p1_sts, p1_acts, p1_sts1, p1_rws, terminal1, episode_rew1, ep1 = q1.get()
            p2_sts, p2_acts, p2_sts1, p2_rws, terminal2, episode_rew2, ep2 = q2.get()

            ep1, step1 = ep1
            ep2, step2 = ep2

            assert((ep1 == ep2) and (step1 == step2))

            maddpg.episode_done = ep1
            print("MAIN LOOP", maddpg.episode_done)

            sts = torch.stack([p1_sts, p2_sts])
            acts = torch.stack([p1_acts, p2_acts])
            sts1 = torch.stack([p1_sts1, p2_sts1])

            rws = np.stack([p1_rws, p2_rws])
            rws = torch.FloatTensor(rws)
            if GPUENABLED:
                maddpg.memory.push(sts.cuda(), acts.cuda(),
                                   sts1.cuda(), rws.cuda())
            else:
                maddpg.memory.push(sts, acts, sts1, rws)

            # At The End of each episode log stats and update target
            if (terminal1 != 0):
                # Create lightweight version of MADDPG and send back to processes
                # TODO: ANSHUL. See if there is a better way to do this. Because
                # each process has its own GIL, we need some way of updating the
                # policies in the agent processes.
                copy_maddpg = copy.deepcopy(maddpg)
                copy_maddpg.to_cpu()
                copy_maddpg.memory.memory = []
                copy_maddpg.memory.position = 0
                r1.put(copy_maddpg)
                r2.put(copy_maddpg)

            fdbk1.put(0)
            fdbk2.put(0)

            maddpg.steps_done += 1
            itr += 1
    except Exception as e:
        #subprocess.call('killall -9 rcssserver', shell=True)
        r1.put(None)
        r2.put(None)
        traceback.print_exc()
        raise e


if __name__ == '__main__':
    run()
