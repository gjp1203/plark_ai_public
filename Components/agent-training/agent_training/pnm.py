# +
# #!pip install pycddlib
# #!pip install stable-baselines3
# The following is needed on the DGX:
# #!pip install torch==1.7.1+cu110 torchvision==0.8.2+cu110 torchaudio===0.7.2 -f https://download.pytorch.org/whl/torch_stable.html

import sys
sys.path.insert(1, '/Components/')

import datetime
import numpy as np
import pandas as pd
import os
import glob
import helper
import lp_solve
import matplotlib.pyplot as plt
import time
import itertools
# -

import tensorflow as tf
tf.logging.set_verbosity(tf.logging.ERROR)
import logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


class PNM():

    # Python constructor to initialise the players within the gamespace.
    # These are subsequently used throughout the game.
    def __init__(self, **kwargs):

        # ######################################################################
        # PARAMS
        # ######################################################################

        self.training_steps             = kwargs.get('training_steps', 250) # N training steps per PNM iteration for each agent
        self.payoff_matrix_trials       = kwargs.get('payoff_matrix_trials', 25) # N eval steps per pairing
        self.max_illegal_moves_per_turn = kwargs.get('max_illegal_moves_per_turn', 2)
        normalise                       = kwargs.get('normalise', True) # Normalise observation vector.
        self.max_n_opponents_to_sample  = kwargs.get('max_n_opponents_to_sample', 30) # so 28 max for 7 parallel envs
        self.num_parallel_envs          = kwargs.get('num_parallel_envs', 7) # Used determine envs in VecEnv
        self.model_type                 = kwargs.get('model_type', 'PPO') # 'PPO' instead of 'PPO2' since we are using torch version
        self.policy                     = kwargs.get('policy', 'MlpPolicy') # Feature extractors
        self.parallel                   = kwargs.get('parallel', True) # Keep it true while working with PPO
        config_file_path                = kwargs.get('config_file_path', '/Components/plark-game/plark_game/game_config/10x10/balanced.json')
        self.basicdate                  = kwargs.get('basicdate', str(datetime.datetime.now().strftime("%Y%m%d_%H%M%S")))
        basepath                        = kwargs.get('basepath', '/data/agents/models')
        self.initial_pelicans           = kwargs.get('initial_pelicans', []) # Specify paths to existing agents if available.
        self.initial_panthers           = kwargs.get('initial_panthers', []) # '' ''
        self.retraining_prob            = kwargs.get('retraining_prob', 0.8) # Probability with which a policy is bootstrapped.
        self.max_pnm_iterations         = kwargs.get('max_pnm_iterations', 100) # N PNM iterations
        self.stopping_eps               = kwargs.get('stopping_eps', 0.001) # required quality of RB-NE
        sparse                          = kwargs.get('sparse', False) # Set to true for sparse rewards.

        # Path to experiment folder
        exp_name = 'test_' + self.basicdate
        self.exp_path = os.path.join(basepath, exp_name)
        logger.info(self.exp_path)

        # Models are saved to:
        self.pelicans_tmp_exp_path = os.path.join(self.exp_path, 'pelicans_tmp')
        os.makedirs(self.pelicans_tmp_exp_path, exist_ok = True)
        self.panthers_tmp_exp_path = os.path.join(self.exp_path, 'panthers_tmp')
        os.makedirs(self.panthers_tmp_exp_path, exist_ok = True)

        # Logs are saved to:
        self.pnm_logs_exp_path = '/data/pnm_logs/test_' + self.basicdate
        os.makedirs(self.pnm_logs_exp_path, exist_ok = True)

        # Initialise sets
        self.pelicans = []
        self.panthers = []

        # Initial models set to None
        self.panther_model = None
        self.pelican_model = None

        self.pelican_training_steps = 0
        self.panther_training_steps = 0

        # Initialize the payoffs
        self.payoffs = np.zeros((1, 1))

        # Creating pelican env
        self.pelican_env = helper.get_envs('pelican',
                                           config_file_path,
                                           num_envs = self.num_parallel_envs,
                                           random_panther_start_position = True,
                                           max_illegal_moves_per_turn = self.max_illegal_moves_per_turn,
                                           sparse = sparse,
                                           vecenv = self.parallel,
                                           normalise = normalise)

        # Creating panther env
        self.panther_env = helper.get_envs('panther',
                                           config_file_path,
                                           num_envs = self.num_parallel_envs,
                                           random_panther_start_position = True,
                                           max_illegal_moves_per_turn = self.max_illegal_moves_per_turn,
                                           sparse = sparse,
                                           vecenv = self.parallel,
                                           normalise = normalise)

    def compute_initial_payoffs(self):
        # If I appended multiple entries all together
        if len(self.initial_pelicans) > 0:
            self.pelicans = self.pelicans[0]
        if len(self.initial_panthers) > 0:
            self.panthers = self.panthers[0]
        # If it is the first iteration and we are starting with initial models we need to build the corresponding payoff
        # Left out the last one for each (added in the normal cycle flow)
        # As we may start with a different number of agents per set, we need to deal with this
        for j, (pelican, panther) in enumerate(itertools.zip_longest(self.pelicans[:-1], self.panthers[:-1])):
            if pelican is not None:
                path = glob.glob(pelican + "/*.zip")[0]
                self.pelican_model = helper.loadAgent(path, self.model_type)
            else:
                self.pelican_model = None
            if panther is not None:
                path = glob.glob(panther + "/*.zip")[0]
                self.panther_model = helper.loadAgent(path, self.model_type)
            else:
                self.panther_model = None
            self.compute_payoff_matrix(self.pelicans[:min(j + 1, len(self.pelicans))],
                                       self.panthers[:min(j + 1, len(self.panthers))])

    def compute_payoff_matrix(self, pelicans, panthers):
        """
        - Pelican strategies are rows; panthers are columns
        - Payoffs are all to the pelican
        """

        # Resizing the payoff matrix for new strategies
        self.payoffs = np.pad(self.payoffs,
                             [(0, len(pelicans) - self.payoffs.shape[0]),
                             (0, len(panthers) - self.payoffs.shape[1])],
                             mode = 'constant')

        # Adding payoff for the last row strategy
        if self.pelican_model is not None:
            for i, opponent in enumerate(panthers):
                self.pelican_env.env_method('set_panther_using_path', opponent)
                victory_prop, avg_reward = helper.check_victory(self.pelican_model,
                                                                self.pelican_env,
                                                                trials = self.payoff_matrix_trials)
                self.payoffs[-1, i] = victory_prop

        # Adding payoff for the last column strategy
        if self.panther_model is not None:
            for i, opponent in enumerate(pelicans):
                self.panther_env.env_method('set_pelican_using_path', opponent)
                victory_prop, avg_reward = helper.check_victory(self.panther_model,
                                                                self.panther_env,
                                                                trials = self.payoff_matrix_trials)
                self.payoffs[i, -1] = 1 - victory_prop # do in terms of pelican

    def train_agent_against_mixture(self,
                                    exp_path,
                                    driving_agent, # agent that we train
                                    model,
                                    env, # Can either be a single env or subvecproc
                                    opponent_policy_fpaths, # policies of opponent of driving agent
                                    opponent_mixture, # mixture of opponent of driving agent
                                    previous_steps):

        ################################################################
        # Heuristic to compute number of opponents to sample as mixture
        ################################################################
        # Min positive probability
        min_prob = min([pr for pr in opponent_mixture if pr > 0])
        target_n_opponents = self.num_parallel_envs * int(1.0 / min_prob)
        n_opponents = min(target_n_opponents, self.max_n_opponents_to_sample)

        if self.parallel:
            # Ensure that n_opponents is a multiple of
            n_opponents = self.num_parallel_envs * round(n_opponents / self.num_parallel_envs)

        logger.info("=============================================")
        logger.info("Sampling %d opponents" % n_opponents)
        logger.info("=============================================")

        # Sample n_opponents
        opponents = np.random.choice(opponent_policy_fpaths,
                                     size = n_opponents,
                                     p = opponent_mixture)

        logger.info("=============================================")
        logger.info("Opponents has %d elements" % len(opponents))
        logger.info("=============================================")

        # If we use parallel envs, we run all the training against different sampled opponents in parallel
        if self.parallel:
            # Method to load new opponents via filepath
            setter = 'set_panther_using_path' if driving_agent == 'pelican' else 'set_pelican_using_path'
            for i, opponent in enumerate(opponents):
                # Stick this in the right slot, looping back after self.num_parallel_envs
                env.env_method(setter, opponent, indices = [i % self.num_parallel_envs])
                # When we have filled all self.num_parallel_envs, then train
                if i > 0 and (i + 1) % self.num_parallel_envs == 0:
                    logger.info("Beginning parallel training for {} steps".format(self.training_steps))
                    model.set_env(env)
                    model.learn(self.training_steps)
                    previous_steps += self.training_steps

        # Otherwise we sample different opponents and we train against each of them separately
        else:
            for opponent in opponents:
                if driving_agent == 'pelican':
                    env.set_panther_using_path(opponent)
                else:
                    env.set_pelican_using_path(opponent)
                logger.info("Beginning sequential training for {} steps".format(self.training_steps))
                model.set_env(env)
                model.learn(self.training_steps)
                previous_steps += self.training_steps

        # Save agent
        logger.info('Finished train agent')
        savepath = self.basicdate + '_steps_' + str(previous_steps)
        agent_filepath, _, _= helper.save_model_with_env_settings(exp_path, model, self.model_type, env, savepath)
        agent_filepath = os.path.dirname(agent_filepath)
        return agent_filepath

    def train_agent(self,
                    exp_path,        # Path for saving the agent
                    model,
                    env,             # Can be either single env or vec env
                    previous_steps): # Used to keep track of number of steps in total

        logger.info("Beginning individual training for {} steps".format(self.training_steps))
        model.set_env(env)
        model.learn(self.training_steps)

        logger.info('Finished train agent')
        savepath = self.basicdate + '_steps_' + str(previous_steps)
        agent_filepath ,_, _= helper.save_model_with_env_settings(exp_path, model, self.model_type, env, savepath)
        agent_filepath = os.path.dirname(agent_filepath)
        previous_steps += self.training_steps
        return agent_filepath


    def initialAgents(self):
        # If no initial pelican agent is given, we train one from fresh
        if len(self.initial_pelicans) == 0:
            # Train initial pelican vs default panther
            self.pelican_model = helper.make_new_model(self.model_type,
                                                       self.policy,
                                                       self.pelican_env,
                                                       n_steps=self.training_steps)
            logger.info('Training initial pelican')
            pelican_agent_filepath = self.train_agent(self.pelicans_tmp_exp_path,
                                                      self.pelican_model,
                                                      self.pelican_env,
                                                      self.pelican_training_steps)
        else:
            logger.info('Initial set of %d pelicans found' % (len(self.initial_pelicans)))
            pelican_agent_filepath = self.initial_pelicans


        # If no initial panther agent is given, we train one from fresh
        if len(self.initial_panthers) == 0:
            # Train initial panther agent vs default pelican
            self.panther_model = helper.make_new_model(self.model_type,
                                                       self.policy,
                                                       self.panther_env,
                                                       n_steps=self.training_steps)
            logger.info('Training initial panther')
            panther_agent_filepath  = self.train_agent(self.panthers_tmp_exp_path,
                                                       self.panther_model,
                                                       self.panther_env,
                                                       self.panther_training_steps)
        else:
            logger.info('Initial set of %d panthers found' % (len(self.initial_panthers)))
            panther_agent_filepath = self.initial_panthers

        return panther_agent_filepath, pelican_agent_filepath

    def run_pnm(self):

        panther_agent_filepath, pelican_agent_filepath = self.initialAgents()

        # Initialize old NE stuff for stopping criterion
        value_to_pelican = 0.
        mixture_pelicans = np.array([1.])
        mixture_panthers = np.array([1.])
        # Create DataFrame for plotting purposes
        df_cols = ["NE_Payoff", "Pelican_BR_Payoff", "Panther_BR_Payoff", "Pelican_supp_size", "Panther_supp_size"]
        df = pd.DataFrame(columns = df_cols)

        # Train best responses until Nash equilibrium is found or max_iterations are reached
        logger.info('Parallel Nash Memory (PNM)')
        for i in range(self.max_pnm_iterations):
            start = time.time()

            logger.info("*********************************************************")
            logger.info('PNM iteration ' + str(i + 1) + ' of ' + str(self.max_pnm_iterations))
            logger.info("*********************************************************")

            self.pelicans.append(pelican_agent_filepath)
            self.panthers.append(panther_agent_filepath)

            if i == 0:
                self.compute_initial_payoffs()

            # Computing the payoff matrices and solving the corresponding LPs
            # Only compute for pelican in the sparse env, that of panther is the negative traspose (game is zero-sum)
            logger.info('Computing payoffs and mixtures')
            self.compute_payoff_matrix(self.pelicans, self.panthers)
            logger.info("=================================================")
            logger.info("New matrix game:")
            logger.info("As numpy array:")
            logger.info('\n' + str(self.payoffs))
            logger.info("As dataframe:")
            tmp_df = pd.DataFrame(self.payoffs).rename_axis('Pelican', axis = 0).rename_axis('Panther', axis = 1)
            logger.info('\n' + str(tmp_df))

            # save payoff matrix
            np.save('%s/payoffs_%d.npy' % (self.pnm_logs_exp_path, i), self.payoffs)

            def get_support_size(mixture):
                # return size of the support of mixed strategy mixture
                return sum([1 if m > 0 else 0 for m in mixture])

            # Check if we found a stable NE, in that case we are done (and fitting DF)
            if i > 0:
                # Both BR payoffs (from against last time's NE) in terms of pelican payoff
                br_value_pelican = np.dot(mixture_pelicans, self.payoffs[-1, :-1])
                br_value_panther = np.dot(mixture_panthers, self.payoffs[:-1, -1])

                ssize_pelican = get_support_size(mixture_pelicans)
                ssize_panther = get_support_size(mixture_panthers)

                logger.info("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
                logger.info("\n\
                             Pelican BR payoff: %.3f,\n\
                             Value of Game: %.3f,\n\
                             Panther BR payoff: %.3f,\n\
                             Pelican Supp Size: %d,\n\
                             Panther Supp Size: %d,\n" % (
                                                          br_value_pelican,
                                                          value_to_pelican,
                                                          br_value_panther,
                                                          ssize_pelican,
                                                          ssize_panther
                                                          ))
                logger.info("@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@")
                values = dict(zip(df_cols, [value_to_pelican, br_value_pelican,
                                                              br_value_panther,
                                                              ssize_pelican,
                                                              ssize_panther]))
                df = df.append(values, ignore_index = True)

                # Write to csv file
                df_path =  os.path.join(self.exp_path, 'values_iter_%02d.csv' % i)
                df.to_csv(df_path, index = False)
                helper.get_fig(df)
                fig_path = os.path.join(self.exp_path, 'values_iter_%02d.pdf' % i)
                plt.savefig(fig_path)
                print("==========================================")
                print("WRITTEN DF TO CSV: %s" % df_path)
                print("==========================================")

                # here value_to_pelican is from the last time the subgame was solved
                if abs(br_value_pelican - value_to_pelican) < self.stopping_eps and\
                   abs(br_value_panther - value_to_pelican) < self.stopping_eps:

                    print('Stable Nash Equilibrium found')
                    break

            logger.info("SOLVING NEW GAME:")
            # solve game for pelican
            (mixture_pelicans, value_to_pelican) = lp_solve.solve_zero_sum_game(self.payoffs)
            # with np.printoptions(precision=3):
            logger.info(mixture_pelicans)
            mixture_pelicans /= np.sum(mixture_pelicans)
            # with np.printoptions(precision=3):
            logger.info("After normalisation:")
            logger.info(mixture_pelicans)
            np.save('%s/mixture_pelicans_%d.npy' % (self.pnm_logs_exp_path, i), mixture_pelicans)

            # solve game for panther
            (mixture_panthers, value_panthers) = lp_solve.solve_zero_sum_game(-self.payoffs.transpose())
            # with np.printoptions(precision=3):
            logger.info(mixture_panthers)
            mixture_panthers /= np.sum(mixture_panthers)
            # with np.printoptions(precision=3):
            logger.info("After normalisation:")
            logger.info(mixture_panthers)
            np.save('%s/mixture_panthers_%d.npy' % (self.pnm_logs_exp_path, i), mixture_panthers)

            # end of logging matrix game and solution
            logger.info("=================================================")

            # Train from skratch or retrain an existing model for pelican
            logger.info('Training pelican')
            if np.random.rand(1) < self.retraining_prob:
                path = np.random.choice(self.pelicans, 1, p = mixture_pelicans)[0]
                path = glob.glob(path + "/*.zip")[0]
                self.pelican_model = helper.loadAgent(path, self.model_type)
            else:
                self.pelican_model = helper.make_new_model(self.model_type,
                                                           self.policy,
                                                           self.pelican_env,
                                                           n_steps=self.training_steps)

            pelican_agent_filepath = self.train_agent_against_mixture('pelican',
                                                                      self.pelicans_tmp_exp_path,
                                                                      self.pelican_model,
                                                                      self.pelican_env,
                                                                      self.panthers,
                                                                      mixture_panthers,
                                                                      self.pelican_training_steps)

            # Train from scratch or retrain an existing model for panther
            logger.info('Training panther')
            if np.random.rand(1) < self.retraining_prob:
                path = np.random.choice(self.panthers, 1, p = mixture_panthers)[0]
                path = glob.glob(path + "/*.zip")[0]
                self.panther_model = helper.loadAgent(path, self.model_type)
            else:
                self.panther_model = helper.make_new_model(self.model_type,
                                                           self.policy,
                                                           self.panther_env,
                                                           n_steps=self.training_steps)

            panther_agent_filepath = self.train_agent_against_mixture('panther',
                                                                     self.panthers_tmp_exp_path,
                                                                     self.panther_model,
                                                                     self.panther_env,
                                                                     self.pelicans,
                                                                     mixture_pelicans,
                                                                     self.panther_training_steps)

            logger.info("PNM iteration lasted: %d seconds" % (time.time() - start))

            # occasionally ouput useful things along the way
            if i == 2:
                # Make video
                video_path =  os.path.join(self.exp_path, 'test_pnm_iter_%d.mp4' % i)
                basewidth,hsize = helper.make_video(self.pelican_model, self.pelican_env, video_path)

        logger.info('Training pelican total steps: ' + str(self.pelican_training_steps))
        logger.info('Training panther total steps: ' + str(self.panther_training_steps))
        # Store DF for printing
        df_path = os.path.join(self.exp_path, "values.csv")
        df.to_csv(df_path, index = False)
        # Make video
        video_path =  os.path.join(self.exp_path, 'test_pnm.mp4')
        basewidth,hsize = helper.make_video(self.pelican_model, self.pelican_env, video_path)

        # Saving final mixture and corresponding agents
        support_pelicans = np.nonzero(mixture_pelicans)[0]
        mixture_pelicans = mixture_pelicans[support_pelicans]
        np.save(self.exp_path + '/mixture_pelicans.npy', mixture_pelicans)
        for i, idx in enumerate(mixture_pelicans):
            self.pelican_model = helper.loadAgent(self.pelicans[i], self.model_type)
            agent_filepath ,_, _= helper.save_model_with_env_settings(self.pelicans_tmp_exp_path,
                                                                      self.pelican_model,
                                                                      self.model_type,
                                                                      self.pelican_env,
                                                                      self.basicdate + "_ps_" + str(i))
        support_panthers = np.nonzero(mixture_panthers)[0]
        mixture_panthers = mixture_panthers[support_panthers]
        np.save(self.exp_path + '/mixture_panthers.npy', mixture_panthers)
        for i, idx in enumerate(mixture_panthers):
            self.panther_model = helper.loadAgent(self.panthers[i], self.model_type)
            agent_filepath ,_, _= helper.save_model_with_env_settings(self.panthers_tmp_exp_path,
                                                                      self.panther_model,
                                                                      self.model_type,
                                                                      self.panther_env,
                                                                      self.basicdate + "_ps_" + str(i))
        return video_path, basewidth, hsize


def main():
    #examples_dir = '/data/examples'
    # Initial sets of opponents is automatically loaded from dir
    #pelicans_start_opponents = [p.path for p in os.scandir(examples_dir + '/pelicans') if p.is_dir()]
    #panthers_start_opponents = [p.path for p in os.scandir(examples_dir + '/panthers') if p.is_dir()]
    #pelicans_start_opponents = ["data/examples/pelicans_tmp/PPO_20210220_152444_steps_" + str(i * 100) + "_pelican" for i in range(1, 7)]
    #panthers_start_opponents = ["data/examples/panthers_tmp/PPO_20210220_152444_steps_" + str(i * 100) + "_panther" for i in range(1, 7)]
    pelicans_start_opponents = []
    panthers_start_opponents = []

    pnm = PNM(initial_pelicans = pelicans_start_opponents,
              initial_panthers = panthers_start_opponents)

    pnm.run_pnm()

if __name__ == '__main__':
    main()
