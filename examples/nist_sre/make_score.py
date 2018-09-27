from __future__ import print_function, division, absolute_import

import os
os.environ['ODIN'] = 'cpu=4,float32,gpu'
import pickle
from collections import OrderedDict, defaultdict

import numpy as np
import tensorflow as tf

from odin.ml import PLDA
from odin import preprocessing as pp
from odin import fuel as F, nnet as N, backend as K
from odin.utils import (get_module_from_path, get_script_path, ctext,
                        Progbar)

from helpers import (SCORING_DATASETS, SCORE_SYSTEM_NAME, SCORE_SYSTEM_ID,
                     PATH_ACOUSTIC_FEATURES, FEATURE_RECIPE, EXP_DIR,
                     get_model_path, NCPU, get_logpath, prepare_dnn_feeder_recipe,
                     sre_file_list, Config, BACKEND_DATASET)

# ====== this folder store extracted vectors for trials and enroll ====== #
SCORE_DIR = os.path.join(EXP_DIR, 'scores')
if not os.path.exists(SCORE_DIR):
  os.mkdir(SCORE_DIR)
# ====== this folder store extracted vectors for training backend ====== #
BACKEND_DIR = os.path.join(EXP_DIR, 'backend')
if not os.path.exists(BACKEND_DIR):
  os.mkdir(BACKEND_DIR)
# ===========================================================================
# Some helper
# ===========================================================================
def _check_running_feature_extraction(feat_dir, feat_name, n_files):
  # True mean need to run the feature extraction
  if not os.path.exists(feat_dir):
    return True
  indices_path = os.path.join(feat_dir, 'indices_%s' % feat_name)
  if not os.path.exists(indices_path):
    return True
  try:
    indices = F.MmapDict(path=indices_path, read_only=True)
    n_indices = len(indices)
    indices.close()
  except Exception as e:
    import traceback
    traceback.print_exc()
    print("Loading indices error: '%s'" % str(e), "at:", indices_path)
    return True
  if n_indices != n_files:
    return True
  return False
# ===========================================================================
# Searching for extractor
# ===========================================================================
extractor_name = FEATURE_RECIPE.split("_")[0]
extractor = get_module_from_path(identifier=extractor_name,
                                 path=get_script_path(),
                                 prefix='feature_recipes')[0]
extractor = extractor()
print(extractor)
# ====== extract the feature if not exists ====== #
scoring_features = {}
for dsname, file_list in sorted(SCORING_DATASETS.items(),
                                key=lambda x: x[0]):
  feat_dir = os.path.join(PATH_ACOUSTIC_FEATURES,
                          '%s_%s' % (dsname, extractor_name))
  log_path = get_logpath(name='%s_%s.log' % (dsname, extractor_name),
                         increasing=True, odin_base=False, root=EXP_DIR)
  # check if need running the feature extraction
  if _check_running_feature_extraction(feat_dir,
                                       feat_name=extractor_name,
                                       n_files=len(file_list)):
    with np.warnings.catch_warnings():
      np.warnings.filterwarnings('ignore')
      processor = pp.FeatureProcessor(jobs=file_list,
                                      path=feat_dir,
                                      extractor=extractor,
                                      ncpu=NCPU,
                                      override=True,
                                      identifier='name',
                                      log_path=log_path,
                                      stop_on_failure=False)
      processor.run()
  # store the extracted dataset
  print("Load dataset:", ctext(feat_dir, 'cyan'))
  scoring_features[dsname] = F.Dataset(path=feat_dir, read_only=True)
# ====== check the duration ====== #
for dsname, ds in scoring_features.items():
  for fname, dur in ds['duration'].items():
    dur = float(dur)
    if dur < 5:
      raise RuntimeError("Dataset: '%s' contains file: '%s', duration='%f' < 5(s)"
        % (dsname, fname, dur))
# ===========================================================================
# Searching for trained system
# ===========================================================================
model_dir, _, _ = get_model_path(system_name=SCORE_SYSTEM_NAME)
model_name = os.path.basename(model_dir)
all_models = []
for path in os.listdir(model_dir):
  path = os.path.join(model_dir, path)
  if 'model.ai.' in path:
    all_models.append(path)
# ====== get the right model based on given system index ====== #
if len(all_models) == 0:
  final_model = os.path.join(model_dir, 'model.ai')
  model_index = ''
  assert os.path.exists(final_model), \
  "Cannot find pre-trained model at path: %s" % model_dir
else:
  all_models = sorted(all_models,
                      key=lambda x: int(x.split('.')[-1]))
  final_model = all_models[SCORE_SYSTEM_ID]
  model_index = final_model[-2:]
# ====== print the log ====== #
print("Found pre-trained at:", ctext(final_model, 'cyan'))
print("Model name :", ctext(model_name, 'cyan'))
print("Model index:", ctext(model_index, 'cyan'))
# just check one more time
assert os.path.exists(final_model), \
"Cannot find pre-trained model at: '%s'" % final_model
# ===========================================================================
# All system must extract following information
# ===========================================================================
# mapping from
# dataset_name -> {'name': 1-D array [n_samples],
#                  'meta': 1-D array [n_samples],
#                  'data': 2-D array [n_samples, n_latent_dim]}
all_scores = {}

# mapping of data for training the backend
# dataset_name -> {'X': 2-D array [n_samples, n_latent_dim],
#                  'y': 1-D array [n_samples]}
all_backend = {}
# ===========================================================================
# Extract the x-vector for enroll and trials
# ===========================================================================
if 'xvec' == SCORE_SYSTEM_NAME:
  # ====== load the network ====== #
  x_vec = N.deserialize(path=final_model,
                        force_restore_vars=True)
  # ====== get output tensors ====== #
  y_logit = x_vec()
  y_proba = tf.nn.softmax(y_logit)
  X = K.ComputationGraph(y_proba).placeholders[0]
  z = K.ComputationGraph(y_proba).get(roles=N.Dense, scope='LatentOutput',
                                      beginning_scope=False)[0]
  f_z = K.function(inputs=X, outputs=z, training=False)
  print('Inputs:', ctext(X, 'cyan'))
  print('Latent:', ctext(z, 'cyan'))
  # ====== recipe for feeder ====== #
  recipe = prepare_dnn_feeder_recipe()
  # ==================== extract x-vector for enroll and trials ==================== #
  for dsname, ds in sorted(scoring_features.items(),
                           key=lambda x: x[0]):
    n_files = len(ds['indices_%s' % extractor_name])
    # ====== check exist scores ====== #
    score_path = os.path.join(SCORE_DIR,
                              '%s%s.%s' % (model_name, model_index, dsname))
    if os.path.exists(score_path):
      with open(score_path, 'rb') as f:
        scores = pickle.load(f)
        if (len(scores['name']) == len(scores['meta']) == len(scores['data']) == n_files):
          all_scores[dsname] = scores
          print(' - Loaded scores at:', ctext(score_path, 'cyan'))
          continue # skip the calculation
    # ====== create feeder ====== #
    feeder = F.Feeder(
        data_desc=F.IndexedData(data=ds[extractor_name],
                                indices=ds['indices_%s' % extractor_name]),
        batch_mode='file', ncpu=8)
    feeder.set_recipes(recipe)
    # ====== init ====== #
    output_name = []
    output_meta = []
    output_data = []
    spkID = ds['spkid'] # metadata stored in spkID
    # progress bar
    prog = Progbar(target=len(feeder), print_summary=True,
                   name=score_path)
    prog.set_summarizer('#File', fn=lambda x: x[-1])
    prog.set_summarizer('#Batch', fn=lambda x: x[-1])
    # ====== make prediction ====== #
    curr_nfile = 0
    for batch_idx, (name, idx, X) in enumerate(feeder.set_batch(
        batch_size=100000, seed=None, shuffle_level=0)):
      assert idx == 0, "File '%s' longer than maximum batch size" % name
      curr_nfile += 1
      z = f_z(X)
      if z.shape[0] > 1:
        z = np.mean(z, axis=0, keepdims=True)
      output_name.append(name)
      output_meta.append(spkID[name])
      output_data.append(z)
      # update the progress
      prog['ds'] = dsname
      prog['name'] = name[:48]
      prog['latent'] = z.shape
      prog['#File'] = curr_nfile
      prog['#Batch'] = batch_idx + 1
      prog.add(X.shape[0])
    # ====== post-processing ====== #
    output_name = np.array(output_name)
    output_meta = np.array(output_meta)
    output_data = np.concatenate(output_data, axis=0)
    # ====== save the score ====== #
    with open(score_path, 'wb') as f:
      scores = {'name': output_name,
                'meta': output_meta,
                'data': output_data.astype('float32')}
      pickle.dump(scores, f)
      all_scores[dsname] = scores
  # ==================== Extract the x-vector for training the backend ==================== #
  assert len(BACKEND_DATASET) > 0, \
  "Datasets for training the backend must be provided"
  print("Backend dataset:", ctext(BACKEND_DATASET, 'cyan'))
  ds = F.Dataset(path=os.path.join(PATH_ACOUSTIC_FEATURES, FEATURE_RECIPE),
                 read_only=True)
  feature_name = FEATURE_RECIPE.split('_')[0]
  ids_name = 'indices_%s' % feature_name
  indices = ds[ids_name]
  indices_dsname = {i: j for i, j in ds['dsname'].items()}
  indices_spkid = {i: j for i, j in ds['spkid'].items()}
  # ====== extract vector for each dataset ====== #
  for dsname in sorted(BACKEND_DATASET):
    path = os.path.join(BACKEND_DIR,
                        model_name + model_index + '.' + dsname)
    print("Processing ...", ctext(os.path.basename(path), 'yellow'))
    # ====== indices ====== #
    indices_ds = [(name, (start, end))
                  for name, (start, end) in indices.items()
                  if indices_dsname[name] == dsname]
    print("  Found: %s (files)" % ctext(len(indices_ds), 'cyan'))
    # skip if no files found
    if len(indices_ds) == 0:
      print("  Skip the calculation!")
      continue
    # ====== found exists vectors ====== #
    if os.path.exists(path):
      with open(path, 'rb') as fin:
        vectors = pickle.load(fin)
        if len(vectors['X']) == len(vectors['y']) and \
        len(vectors['X']) > 0:
          print("  Loaded vectors:",
                ctext(vectors['X'].shape, 'cyan'),
                ctext(vectors['y'].shape, 'cyan'))
          all_backend[dsname] = vectors
          continue
    # ====== create feeder ====== #
    feeder = F.Feeder(
        data_desc=F.IndexedData(data=ds[feature_name],
                                indices=indices_ds),
        batch_mode='file', ncpu=8)
    feeder.set_recipes(recipe)
    prog = Progbar(target=len(feeder), print_summary=True,
                   name="Extracting vector for: %s - %d (files)" %
                   (dsname, len(indices_ds)))
    # ====== extracting vectors ====== #
    Z_out = []
    y_out = []
    for name, idx, X in feeder.set_batch(
        batch_size=100000, seed=None, shuffle_level=0):
      assert idx == 0, "File '%s' longer than maximum batch size" % name
      # get the latent
      z = f_z(X)
      if z.shape[0] > 1:
        z = np.mean(z, axis=0, keepdims=True)
      Z_out.append(z)
      y_out.append(indices_spkid[name])
      # update the progress
      prog['name'] = name[:48]
      prog.add(X.shape[0])
    # ====== post processing ====== #
    Z_out = np.concatenate(Z_out).astype('float32')
    y_out = np.array(y_out)
    with open(path, 'wb') as fout:
      pickle.dump({'X': Z_out,
                   'y': y_out},
                  fout)
    print('  Extracted:', ctext(Z_out.shape, 'cyan'), y_out.shape)
    # ====== store the backend vectors ====== #
    all_backend[dsname] = {'X': Z_out,
                           'y': y_out}
# ===========================================================================
# Extract the i-vector
# ===========================================================================
elif 'ivec' == SCORE_SYSTEM_NAME:
  raise NotImplementedError
# ===========================================================================
# Unknown system
# ===========================================================================
else:
  raise RuntimeError("No support for system: %s" % SCORE_SYSTEM_NAME)
# ===========================================================================
# Prepare data for training the backend
# ===========================================================================
assert len(all_backend) > 0
X_backend = []
y_backend = []
n_speakers = 0
for dsname, vectors in all_backend.items():
  X, y = vectors['X'], vectors['y']
  # add the data
  X_backend.append(X)
  # add the labels
  y_backend += y.tolist()
  # create label list
  n_speakers += len(np.unique(y))
# create mapping of spk to integer label
all_speakers = sorted(set(y_backend))
spk2label = {j: i
             for i, j in enumerate(all_speakers)}
# make sure no overlap speaker among dataset
assert len(all_speakers) == n_speakers
# create the training data
X_backend = np.concatenate(X_backend, axis=0)
y_backend = np.array([spk2label[i] for i in y_backend])
print("Training data for backend:")
print("  #Speakers:", ctext(n_speakers, 'cyan'))
print("  X        :", ctext(X_backend.shape, 'cyan'))
print("  y        :", ctext(y_backend.shape, 'cyan'))
# ===========================================================================
# Now scoring
# ===========================================================================
for dsname, scores in all_scores.items():
  print("Scoring:", ctext(dsname, 'yellow'))
  # load the scores
  seg_name, seg_meta, seg_data = scores['name'], scores['meta'], scores['data']
  name_2_data = {i: j for i, j in zip(seg_name, seg_data)}
  # get the enroll and trials list
  enroll_name = '%s_enroll' % dsname
  trials_name = '%s_trials' % dsname
  if enroll_name in sre_file_list and trials_name in sre_file_list:
    trials = sre_file_list[trials_name]
    enroll = sre_file_list[enroll_name]
    # ====== create the enrollments data ====== #
    models = OrderedDict()
    for model_id, segment_id in enroll[:, :2]:
      if model_id not in models:
        models[model_id] = []
      models[model_id].append(name_2_data[segment_id])
    # calculate the x-vector for each model
    models = OrderedDict([
        (model_id, np.mean(seg_list, axis=0, keepdims=True))
        for model_id, seg_list in models.items()
    ])
    models_name = list(models.keys())
    models_vecs = np.concatenate(list(models.values()), axis=0)
    print("  Enroll model:", ctext(models_vecs.shape, 'cyan'))
    # ====== create the trials list ====== #
    X = np.concatenate([name_2_data[i][None, :] for i in trials[:, 1]],
                       axis=0)
    print("  Trials      :", ctext(X.shape, 'cyan'))
    # ====== training the plda ====== #
    plda = PLDA(n_phi=150,
                centering=True, wccn=True, unit_length=True,
                n_iter=20, random_state=Config.SUPER_SEED,
                verbose=1)
    plda.fit(X=X_backend, y=y_backend)
  else:
    raise RuntimeError(
        "Cannot find '_trials.csv' and '_enroll.csv' for dataset: %s" % dsname)
