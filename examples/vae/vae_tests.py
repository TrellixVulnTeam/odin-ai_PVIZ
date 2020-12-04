from __future__ import absolute_import, division, print_function

import inspect
import os
from functools import partial
from math import sqrt

import numpy as np
import seaborn as sns
import tensorflow as tf
from matplotlib import pyplot as plt
from odin import backend as bk
from odin import visual as vs
from odin.backend import interpolation
from odin.bay.vi import (DisentanglementGym, GroundTruth, NetworkConfig, RVmeta,
                         VariationalAutoencoder, VariationalPosterior, get_vae,
                         traverse_dims, DimReduce, Correlation)
from odin.fuel import IterableDataset, get_dataset
from odin.ml import fast_tsne, fast_umap
from odin.networks import get_networks, get_optimizer_info
from odin.training import get_output_dir, run_hydra
from odin.utils import ArgController, as_tuple, clear_folder
from tensorflow.python import keras
from tqdm import tqdm

try:
  tf.config.experimental.set_memory_growth(
      tf.config.list_physical_devices('GPU')[0], True)
except IndexError:
  pass
tf.debugging.set_log_device_placement(False)
tf.autograph.set_verbosity(0)

tf.random.set_seed(8)
np.random.seed(8)
sns.set()

# ===========================================================================
# Configuration
# Example:
# python all_vae_test.py vae=betavae ds=dsprites beta=1,10,20 px=bernoulli py=onehot max_iter=100000 -m -j4
# ===========================================================================
OUTPUT_DIR = '/tmp/vae_tests'
batch_size = 32
n_visual_samples = 16

CONFIG = \
r"""
vae:
ds:
qz: mvndiag
beta: 1
gamma: 1
alpha: 10
lamda: 1
skip: False
eval: False
"""


# ===========================================================================
# Helpers
# ===========================================================================
def load_data(name: str):
  ds = get_dataset(name)
  test = ds.create_dataset(partition='test',
                           inc_labels=1.0 if ds.has_labels else 0.0)
  samples = [
      [i[:n_visual_samples] for i in tf.nest.flatten(x)] for x in test.take(1)
  ][0]
  if ds.has_labels:
    x_samples, y_samples = samples
  else:
    x_samples = samples[0]
    y_samples = None
  return ds, x_samples, y_samples


def create_gym(dsname: str, vae: VariationalAutoencoder) -> DisentanglementGym:
  gym = DisentanglementGym(dataset=dsname, vae=vae)
  gym.set_config(track_gradients=True,
                 latents_pairs=None,
                 mig_score=True,
                 silhouette_score=True,
                 adjusted_rand_score=True,
                 mode='train')
  gym.set_config(
      latents_pairs=Correlation.Lasso | Correlation.MutualInfo,
      correlation_methods=Correlation.Lasso | Correlation.MutualInfo |
      Correlation.Importance | Correlation.Spearman,
      dimension_reduction=DimReduce.PCA | DimReduce.TSNE | DimReduce.UMAP,
      mig_score=True,
      dci_score=True,
      sap_score=True,
      factor_vae=True,
      beta_vae=True,
      silhouette_score=True,
      adjusted_rand_score=True,
      normalized_mutual_info=True,
      adjusted_mutual_info=True,
      mode='eval')
  return gym


# ===========================================================================
# Main
# ===========================================================================
@run_hydra(output_dir=OUTPUT_DIR, exclude_keys=['eval'])
def main(cfg: dict):
  assert cfg.vae is not None, \
    ('No VAE model given, select one of the following: '
     f"{', '.join(i.__name__.lower() for i in get_vae())}")
  assert cfg.ds is not None, \
    ('No dataset given, select one of the following: '
     'mnist, dsprites, shapes3d, celeba, cortex, newsgroup20, newsgroup5, ...')
  ### paths
  output_dir = get_output_dir()
  gym_train_path = os.path.join(output_dir, 'gym_train')
  gym_eval_path = os.path.join(output_dir, 'gym_eval')
  model_path = os.path.join(output_dir, 'model')
  ### load dataset
  ds, x_samples, y_samples = load_data(name=cfg.ds)
  ds_kw = dict(batch_size=batch_size, drop_remainder=True)
  ### prepare model init
  model = get_vae(cfg.vae)
  model_kw = inspect.getfullargspec(model.__init__).args[1:]
  model_kw = {k: v for k, v in cfg.items() if k in model_kw}
  is_semi_supervised = ds.has_labels and model.is_semi_supervised()
  if is_semi_supervised:
    train = ds.create_dataset(partition='train', inc_labels=0.1, **ds_kw)
    valid = ds.create_dataset(partition='valid', inc_labels=1.0, **ds_kw)
  else:
    train = ds.create_dataset(partition='train', inc_labels=0., **ds_kw)
    valid = ds.create_dataset(partition='valid', inc_labels=0., **ds_kw)
  ### create the model
  vae = model(path=model_path,
              **get_networks(cfg.ds,
                             centerize_image=True,
                             is_semi_supervised=is_semi_supervised,
                             skip_generator=cfg.skip),
              **model_kw)
  vae.build((None,) + x_samples.shape[1:])
  vae.load_weights(raise_notfound=False, verbose=True)
  gym = create_gym(dsname=cfg.ds, vae=vae)
  gym.train()

  ### fit the network
  def callback():
    signal = vae.early_stopping(verbose=True)
    if signal < 0:
      vae.trainer.terminate()
    elif signal > 0:
      vae.save_weights(overwrite=True)
    # create the return metrics
    return gym(save_path=gym_train_path, remove_saved_image=True, dpi=150)

  ### fit
  max_iter, learning_rate = get_optimizer_info(cfg.ds)
  vae.fit(train,
          valid=valid,
          learning_rate=learning_rate,
          epochs=-1,
          clipnorm=100,
          max_iter=max_iter,
          valid_freq=1000,
          logging_interval=2,
          skip_fitted=True,
          callback=callback,
          logdir=output_dir,
          compile_graph=True,
          track_gradients=True)

  ### evaluation
  if cfg.eval:
    gym.eval()
    gym(save_path=gym_eval_path, remove_saved_image=True, dpi=200, verbose=True)


# ===========================================================================
# Run the experiment
# ===========================================================================
if __name__ == "__main__":
  main(CONFIG)
