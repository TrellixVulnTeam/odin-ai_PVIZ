from __future__ import absolute_import, division, print_function

import os
import sys

import numpy as np
import tensorflow as tf
from sklearn.metrics import accuracy_score, classification_report
from tensorflow.python import keras
from tqdm import tqdm

from odin.bay import RandomVariable as RV
from odin.bay.vi import Criticizer
from odin.bay.vi.autoencoder import (Factor2VAE, FactorDiscriminator, FactorVAE,
                                     SemiFactor2VAE, SemiFactorVAE)
from odin.exp import Experimenter
from odin.fuel import get_dataset
from odin.utils import md5_folder

os.environ['TF_CPP_MIN_LOG_LEVEL'] = '3'
os.environ['TF_FORCE_GPU_ALLOW_GROWTH'] = 'true'

tf.random.set_seed(1)
np.random.seed(1)

# ===========================================================================
# Helpers
# vae=factor,factor2 ds=celeba,mnist pretrain=0,1000 finetune=12000 maxtc=True,False
# vae=semi,semi2 ds=celeba,mnist pretrain=0,1000 finetune=12000 alpha=1,10 strategy=logsumexp,max
# python factor_vae.py vae=factor,factor2 ds=cifar10,cifar20,cifar100,mnist pretrain=1000 finetune=10000 -m -ncpu=3;
# python factor_vae.py vae=semi,semi2 ds=cifar10,cifar20,cifar100,mnist pretrain=1000 finetune=10000 semi=0.1,0.01 -m -ncpu=3
# python factor_vae.py vae=factor,factor2 ds=shapes3D,dsprites,celeba pretrain=0,1000 finetune=12000 maxtc=True,False gamma=6,10,20 -m -ncpu=3
# TODO: some things wrong with Shapes3D
# ===========================================================================
CONFIG = \
r"""
ds: mnist
vae: factor
pretrain: 0
finetune: 10000
alpha: 10.
beta: 1.
gamma: 6.
lamda: 1.
semi: 0.1
maxtc: False
strategy: logsumexp
verbose: False
gpu: False
"""


def cal_mllk(vae, data, gpu=False):
  device = 'GPU' if gpu else 'CPU'
  with tf.device(f"/{device}:0"):
    return np.mean(
        np.concatenate([
            vae.marginal_log_prob(x, sample_shape=50)
            for x in tqdm(data.repeat(1), desc="Calculating MarginalLLK")
        ],
                       axis=0))


# ===========================================================================
# Experiments
# ===========================================================================
class Factor(Experimenter):

  def __init__(self):
    super().__init__(save_path='~/exp/factorexp',
                     config_path=CONFIG,
                     exclude_keys=['verbose', 'gpu'],
                     hash_length=5)

  def on_load_data(self, cfg):
    ds = get_dataset(cfg.ds)()
    ds.sample_images(save_path=os.path.join(self.save_path, 'samples.png'))
    kw = dict(batch_size=128, drop_remainder=True)
    train = ds.create_dataset(partition='train',
                              inc_labels=float(cfg.semi),
                              **kw)
    train_u = ds.create_dataset(partition='train', inc_labels=False, **kw)
    valid = ds.create_dataset(partition='valid', inc_labels=1.0, **kw)
    valid_u = ds.create_dataset(partition='valid', inc_labels=False, **kw)
    # reduce batch_size here, otherwise, mllk take ~ 7GB VRAM
    kw['batch_size'] = 8
    test = ds.create_dataset(partition='test', inc_labels=1.0, **kw)
    test_u = ds.create_dataset(partition='test', inc_labels=False, **kw)
    self.ds = ds
    self.train, self.train_u = train, train_u
    self.valid, self.valid_u = valid, valid_u
    self.test, self.test_u = test, test_u
    if cfg.verbose:
      print("Dataset:", ds)
      print(" train:", train)
      print(" train_u:", train_u)

  def on_create_model(self, cfg, model_dir, md5):
    kw = dict(alpha=cfg.alpha,
              beta=cfg.beta,
              gamma=cfg.gamma,
              lamda=cfg.lamda,
              maximize_tc=bool(cfg.maxtc),
              path=os.path.join(model_dir, 'weight'))
    if cfg.vae == 'factor':
      del kw['alpha']
      model = FactorVAE(
          encoder=cfg.ds,
          outputs=RV(self.ds.shape, 'bern', name="Image"),
          latents=RV(20, 'diag', projection=True, name="Latents"),
          **kw,
      )
    elif cfg.vae == 'factor2':
      del kw['alpha']
      model = Factor2VAE(
          encoder=cfg.ds,
          outputs=RV(self.ds.shape, 'bern', name="Image"),
          latents=RV(10, 'diag', projection=True, name='Latents'),
          factors=RV(10, 'diag', projection=True, name='Factors'),
          **kw,
      )
    elif cfg.vae == 'semi':
      model = SemiFactorVAE(
          encoder=cfg.ds,
          outputs=RV(self.ds.shape, 'bern', name="Image"),
          latents=RV(20, 'diag', projection=True, name="Latents"),
          n_labels=self.ds.n_labels,
          ss_strategy=cfg.strategy,
          **kw,
      )
    elif cfg.vae == 'semi2':
      model = SemiFactor2VAE(
          encoder=cfg.ds,
          outputs=RV(self.ds.shape, 'bern', name="Image"),
          latents=RV(10, 'diag', projection=True, name='Latents'),
          factors=RV(10, 'diag', projection=True, name='Factors'),
          n_labels=self.ds.n_labels,
          ss_strategy=cfg.strategy,
          **kw,
      )
    else:
      raise NotImplementedError("No support for model: %s" % cfg.vae)
    # store the model
    self.model = model
    if cfg.verbose:
      print(model)
      print(model_dir)
      if md5 is not None:
        print("MD5 saved: ", md5)
        print("MD5 loaded:", md5_folder(model_dir))

  def on_train(self, cfg, output_dir, model_dir):
    if cfg.verbose:
      print("Configurations:")
      for k, v in cfg.items():
        print('%-10s' % k, ':', v)
    # start training
    kw = {}
    if cfg.pretrain > 0:
      self.model.pretrain().fit(self.train_u,
                                valid=self.valid_u,
                                max_iter=cfg.pretrain)
      kw['optimizer'] = None
    if cfg.finetune > 0:
      train, valid = (self.train, self.valid) if self.model.is_semi_supervised \
        else (self.train_u, self.valid_u)
      self.model.finetune().fit(train, valid=valid, max_iter=cfg.finetune, **kw)
    self.model.plot_learning_curves(os.path.join(output_dir,
                                                 'learning_curves.png'),
                                    title=self.model.__class__.__name__)
    self.model.save_weights(os.path.join(model_dir, 'weight'))

  def on_eval(self, cfg, output_dir):
    # marginal log-likelihood
    mllk = cal_mllk(self.model, self.test_u, gpu=cfg.gpu)
    # Criticizer
    crt = Criticizer(vae=self.model)
    crt.sample_batch(inputs=self.test, n_samples=[10000, 5000], verbose=True)
    # clustering scores
    scores = crt.cal_clustering_scores()
    # downstream scores
    beta = np.mean(crt.cal_betavae_score(n_samples=10000, verbose=True))
    factor = np.mean(crt.cal_factorvae_score(n_samples=10000, verbose=True))
    tc = np.mean(crt.cal_total_correlation())
    d, c, i = crt.cal_dci_scores()
    sap = np.mean(crt.cal_separated_attr_predictability())
    rds_pearson = np.mean(crt.cal_relative_disentanglement_strength("pearson"))
    rds_spearman = np.mean(
        crt.cal_relative_disentanglement_strength("spearman"))
    mig = np.mean(crt.cal_mutual_info_gap())
    dmi, cmi = crt.cal_dcmi_scores()
    # save to database
    scores = dict(
        beta=beta,
        factor=factor,
        sap=sap,
        pearson=rds_pearson,
        spearman=rds_spearman,
        tc=tc,
        d=d,
        c=c,
        i=i,
        dmi=dmi,
        cmi=cmi,
        mig=mig,
        mllk=mllk,
        asw=scores['ASW'],
        ari=scores['ARI'],
        nmi=scores['NMI'],
        uca=scores['UCA'],
    )
    if cfg.verbose:
      for k, v in scores.items():
        print('%-8s' % k, ':', '%.3f' % v)
    self.save_scores(table="score", override=True, **scores)


# ===========================================================================
# Main
# ===========================================================================
if __name__ == "__main__":
  exp = Factor()
  exp.train().run()
