# coding=utf-8
# Copyright 2018 The DisentanglementLib Authors.  All rights reserved.
# (https://github.com/google-research/disentanglement_lib)
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
from __future__ import absolute_import, division, print_function

import warnings

import numpy as np
import scipy as sp
from sklearn.cluster import KMeans
from sklearn.ensemble import GradientBoostingClassifier
from sklearn.metrics import (adjusted_mutual_info_score, adjusted_rand_score,
                             mutual_info_score, normalized_mutual_info_score,
                             silhouette_score)
from sklearn.metrics.cluster import entropy as entropy1D
from sklearn.mixture import GaussianMixture

from odin.bay.vi.downstream_metrics import *
from odin.utils import catch_warnings_ignore
from odin.utils.mpi import MPI, get_cpu_count

__all__ = [
    'discrete_mutual_info',
    'discrete_entropy',
    'mutual_info_score',
    'mutual_info_estimate',
    'mutual_info_gap',
    'representative_importance_matrix',
    'dci_scores',
    # unsupervised scores
    'unsupervised_clustering_scores',
    # downstream score
    'separated_attr_predictability',
    'beta_vae_score',
    'factor_vae_score',
]


# ===========================================================================
# Clustering scores
# ===========================================================================
def unsupervised_clustering_scores(representations,
                                   factors,
                                   prediction_algorithm='both',
                                   seed=1):
  r""" Calculating the unsupervised clustering Scores:
    - ASW: silhouette_score (higher is better, best is 1, worst is -1)
    - ARI: adjusted_rand_score (higher is better)
    - NMI: normalized_mutual_info_score (higher is better)
    - UCA: unsupervised_clustering_accuracy (higher is better)

  Note: remember the order of returned value

  Arguments:
    factors : a Matrix. Categorical factors (i.e. one-hot encoded), or multiple
      factors
    prediction_algorithm : {'knn', 'gmm', 'both'}. The algorithm for
      predicting factors from representations

  Return:
    dict(ASW=asw_score, ARI=ari_score, NMI=nmi_score, UCA=uca_score)

  """
  # simple normalization to 0-1, then pick the argmax
  if factors.ndim == 2:
    vmin = np.min(factors, axis=0, keepdims=True)
    vmax = np.max(factors, axis=0, keepdims=True)
    factors = (factors - vmin) / (vmax - vmin)
    factors = np.argmax(factors, axis=-1)
  if prediction_algorithm == 'knn':
    km = KMeans(n_factors, n_init=200, random_state=seed)
    factors_pred = km.fit_predict(representations)
  elif prediction_algorithm == 'gmm':
    gmm = GaussianMixture(n_factors, random_state=seed)
    gmm.fit(representations)
    factors_pred = gmm.predict(representations)
  elif prediction_algorithm == 'both':
    score1 = clustering_scores(representations,
                               factors,
                               n_factors=n_factors,
                               prediction_algorithm='knn')
    score2 = clustering_scores(representations,
                               factors,
                               n_factors=n_factors,
                               prediction_algorithm='gmm')
    return {k: (v + score2[k]) / 2 for k, v in score1.items()}
  else:
    raise ValueError("Not support for prediction_algorithm: '%s'" %
                     prediction_algorithm)
  #
  with catch_warnings_ignore(FutureWarning):
    asw_score = silhouette_score(representations, factors)
    ari_score = adjusted_rand_score(factors, factors_pred)
    nmi_score = normalized_mutual_info_score(factors, factors_pred)
    uca_score = unsupervised_clustering_accuracy(factors, factors_pred)[0]
  return dict(ASW=asw_score, ARI=ari_score, NMI=nmi_score, UCA=uca_score)


# ===========================================================================
# Mutual information
# ===========================================================================
def discrete_mutual_info(codes, factors):
  r"""Compute discrete mutual information.

  Arguments:
    codes : `[n_samples, n_codes]`, the latent codes or predictive codes
    factors : `[n_samples, n_factors]`, the groundtruth factors

  Return:
    matrix `[n_codes, n_factors]` : mutual information score between factor
      and code
  """
  codes = np.atleast_2d(codes)
  factors = np.atleast_2d(factors)
  assert codes.ndim == 2 and factors.ndim == 2, \
    "codes and factors must be matrix, but given: %s and %s" % \
      (str(codes.shape), str(factors.shape))
  num_latents = codes.shape[1]
  num_factors = factors.shape[1]
  m = np.zeros([num_latents, num_factors])
  for i in range(num_latents):
    for j in range(num_factors):
      m[i, j] = mutual_info_score(factors[:, j], codes[:, i])
  return m


def discrete_entropy(labels):
  r""" Iterately compute discrete entropy for integer samples set along the
  column of 2-D array.

  Arguments:
    labels : 1-D or 2-D array

  Returns:
    entropy : A Scalar or array `[n_factors]`
  """
  labels = np.atleast_1d(labels)
  if labels.ndim == 1:
    return entropy1D(labels.ravel())
  elif labels.ndim > 2:
    raise ValueError("Only support 1-D or 2-D array for labels entropy.")
  num_factors = labels.shape[1]
  h = np.zeros(num_factors)
  for j in range(num_factors):
    h[j] = entropy1D(labels[:, j])
  return h


def mutual_info_estimate(representations,
                         factors,
                         continuous_representations=True,
                         continuous_factors=False,
                         n_neighbors=3,
                         random_state=1234):
  r""" Nonparametric method for estimating entropy from k-nearest neighbors
  distances (note: this implementation use multi-processing)

  Return:
    matrix `[num_latents, num_factors]`, estimated mutual information between
      each representation and each factors

  References:
    A. Kraskov, H. Stogbauer and P. Grassberger, “Estimating mutual information”.
      Phys. Rev. E 69, 2004.
    B. C. Ross “Mutual Information between Discrete and Continuous Data Sets”.
      PLoS ONE 9(2), 2014.
    L. F. Kozachenko, N. N. Leonenko, “Sample Estimate of the Entropy of a
      Random Vector:, Probl. Peredachi Inf., 23:2 (1987), 9-16
  """
  from sklearn.feature_selection import (mutual_info_classif,
                                         mutual_info_regression)
  mutual_info = mutual_info_regression if continuous_factors else \
    mutual_info_classif
  num_latents = representations.shape[1]
  num_factors = factors.shape[1]
  # iterate over each factor
  mi_matrix = np.empty(shape=(num_latents, num_factors), dtype=np.float64)

  # repeat for each factor
  def func(idx):
    mi = mutual_info(representations,
                     factors[:, idx],
                     discrete_features=not continuous_representations,
                     n_neighbors=n_neighbors,
                     random_state=random_state)
    return idx, mi

  for i, mi in MPI(jobs=list(range(num_factors)),
                   func=func,
                   ncpu=max(1,
                            get_cpu_count() - 1),
                   batch=1):
    mi_matrix[:, i] = mi
  return mi_matrix


def mutual_info_gap(representations, factors):
  r"""Computes score based on both representation codes and factors.
    In (Chen et. al 2019), 10000 samples used to estimate MIG

  Arguments:
    representation : `[n_samples, n_latents]`, discretized latent
      representation
    factors : `[n_samples, n_factors]`, discrete groundtruth factor

  Return:
    A scalar: discrete mutual information gap score

  Reference:
    Chen, R.T.Q., Li, X., Grosse, R., Duvenaud, D., 2019. Isolating Sources of
      Disentanglement in Variational Autoencoders. arXiv:1802.04942 [cs, stat].

  """
  representations = np.atleast_2d(representations).astype(np.int64)
  factors = np.atleast_2d(factors).astype(np.int64)
  # m is [n_latents, n_factors]
  m = discrete_mutual_info(representations, factors)
  sorted_m = np.sort(m, axis=0)[::-1]
  entropy_ = discrete_entropy(factors)
  return np.mean(np.divide(sorted_m[0, :] - sorted_m[1, :], entropy_[:]))


# ===========================================================================
# Disentanglement, completeness, informativeness
# ===========================================================================
def disentanglement_score(importance_matrix):
  r""" Compute the disentanglement score of the representation.

  Arguments:
    importance_matrix : is of shape `[num_latents, num_factors]`.
  """
  per_code = 1. - sp.stats.entropy(
      importance_matrix + 1e-11, base=importance_matrix.shape[1], axis=1)
  if importance_matrix.sum() == 0.:
    importance_matrix = np.ones_like(importance_matrix)
  code_importance = importance_matrix.sum(axis=1) / importance_matrix.sum()
  return np.sum(per_code * code_importance)


def completeness_score(importance_matrix):
  r""""Compute completeness of the representation.

  Arguments:
    importance_matrix : is of shape `[num_latents, num_factors]`.
  """
  per_factor = 1. - sp.stats.entropy(
      importance_matrix + 1e-11, base=importance_matrix.shape[0], axis=0)
  if importance_matrix.sum() == 0.:
    importance_matrix = np.ones_like(importance_matrix)
  factor_importance = importance_matrix.sum(axis=0) / importance_matrix.sum()
  return np.sum(per_factor * factor_importance)


def representative_importance_matrix(repr_train,
                                     factor_train,
                                     repr_test,
                                     factor_test,
                                     random_state=1234,
                                     algo=GradientBoostingClassifier):
  r""" Using Gradient Boosting to estimate the importance of each
  representation for each factor.

  Arguments:
    algo : `sklearn.Estimator`, a classifier with `feature_importances_`
      attribute, for example:
        averaging methods:
        - `sklearn.ensemble.ExtraTreesClassifier`
        - `sklearn.ensemble.RandomForestClassifier`
        - `sklearn.ensemble.IsolationForest`
        and boosting methods:
        - `sklearn.ensemble.GradientBoostingClassifier`
        - `sklearn.ensemble.AdaBoostClassifier`
  """
  num_latents = repr_train.shape[1]
  num_factors = factor_train.shape[1]
  assert hasattr(algo, 'feature_importances_'), \
    "The class must contain 'feature_importances_' attribute"

  def _train(factor_idx):
    model = algo(random_state=random_state)
    model.fit(repr_train, factor_train[:, factor_idx])
    feat = np.abs(model.feature_importances_)
    train = np.mean(model.predict(repr_train) == factor_train[:, factor_idx])
    test = np.mean(model.predict(repr_test) == factor_test[:, factor_idx])
    return factor_idx, feat, train, test

  # ====== compute importance based on gradient boosted trees ====== #
  importance_matrix = np.zeros(shape=[num_latents, num_factors],
                               dtype=np.float64)
  train_acc = list(range(num_factors))
  test_acc = list(range(num_factors))
  for i, feat, train, test, in MPI(jobs=list(range(num_factors)),
                                   func=_train,
                                   batch=1,
                                   ncpu=max(1,
                                            get_cpu_count() - 1)):
    importance_matrix[:, i] = feat
    train_acc[i] = train
    test_acc[i] = test
  return importance_matrix, train_acc, test_acc


def dci_scores(repr_train,
               factor_train,
               repr_test,
               factor_test,
               random_state=1234):
  r""" Disentanglement, completeness, informativeness

  Arguments:
    repr_train, repr_test : 2-D matrix `[n_samples, latent_dim]`
    factor_train, factor_test : 2-D matrix `[n_samples, n_factors]`

  Return:
    tuple of 3 scores (disentanglement, completeness, informativeness), all
      scores are higher is better.
      - disentanglement score: The degree to which a representation factorises
        or disentangles the underlying factors of variatio
      - completeness score: The degree to which each underlying factor is
        captured by a single code variable.
      - informativeness score: test accuracy of a factor recognizer trained
        on train data

  References:
    Based on "A Framework for the Quantitative Evaluation of Disentangled
    Representations" (https://openreview.net/forum?id=By-7dz-AZ).

  Note:
    This impelentation only return accuracy on test data as informativeness
      score
  """
  importance, train_acc, test_acc = representative_importance_matrix(
      repr_train, factor_train, repr_test, factor_test, random_state)
  train_acc = np.mean(train_acc)
  test_acc = np.mean(test_acc)
  # ====== disentanglement and completeness ====== #
  d = disentanglement_score(importance)
  c = completeness_score(importance)
  i = test_acc
  return d, c, i


def relative_strength(mat):
  r""" Computes relative strength score for both axes of a correlation matrix.

  Arguments:
    mat : a Matrix. Correlation matrix with values range from -1 to 1.
  """
  with warnings.catch_warnings():
    warnings.filterwarnings('ignore', category=RuntimeWarning)
    score_x = np.mean(np.nan_to_num(\
      np.power(np.max(mat, axis=0), 2) / np.sum(mat, axis=0),
      copy=False, nan=0.0))
    score_y = np.mean(np.nan_to_num(\
      np.power(np.max(mat, axis=1), 2) / np.sum(mat, axis=1),
      copy=False, nan=0.0))
  return (score_x + score_y) / 2
