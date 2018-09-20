from __future__ import print_function, division, absolute_import
import matplotlib
matplotlib.use('Agg')

import os
os.environ['ODIN'] = 'float32,gpu'
import sys
from shutil import which

import numpy as np

import soundfile as sf

from odin import visual as V, nnet as N
from odin.utils import (ctext, unique_labels, Progbar, UnitTimer,
                        stdio, Progbar)
from odin import fuel as F, preprocessing as pp
from odin.stats import sampling_iter

from helpers import (PATH_ACOUSTIC_FEATURES, EXP_DIR,
                     ALL_FILES, ALL_DATASET,
                     Config, IS_DEBUGGING)

stdio(os.path.join(EXP_DIR, 'features_extraction.log'))
# ===========================================================================
# Customized Extractor
# ===========================================================================
class SREAudioReader(pp.base.Extractor):
  """ SREAudioReader """

  def __init__(self):
    super(SREAudioReader, self).__init__(is_input_layer=True)

  def _transform(self, row):
    # `row`:
    #  0       1      2      3       4          5         6
    # path, channel, name, spkid, dataset, start_time, end_time
    path, channel, name, spkid, dataset, start_time, end_time = row
    if start_time == '-':
      start_time = None
    if end_time == '-':
      end_time = None
    # ====== read audio ====== #
    if dataset == 'voxceleb1':
      with open(path, 'rb') as f:
        y, sr = sf.read(f)
        y = pp.signal.resample(y, sr_orig=sr, sr_new=8000)
        sr = 8000
    elif dataset[:3] == 'sre' or dataset == 'swb' or dataset == 'fisher':
      with open(path, 'rb') as f:
        y, sr = sf.read(f)
        y = pp.signal.resample(y, sr_orig=sr, sr_new=8000)
        if y.ndim == 2:
          y = y[:, int(channel)]
        sr = 8000
    else:
      y, sr = pp.signal.anything2wav(inpath=path, outpath=None,
                                     channel=channel,
                                     dataset=dataset,
                                     start=start_time, end=end_time,
                                     sample_rate=Config.SAMPLE_RATE,
                                     return_data=True)
    # ====== error happen ignore file ====== #
    if len(y) == 0:
      return None
    # ====== remove DC offset ====== #
    y = y - np.mean(y, 0)
    duration = max(y.shape) / sr
    return {'raw': y, 'sr': sr, 'duration': duration, # in second
            'path': path, 'spkid': spkid, 'name': name,
            'ds': dataset}
# ===========================================================================
# Extractor
# ===========================================================================
extractors = pp.make_pipeline(steps=[
    SREAudioReader(),
    pp.speech.PreEmphasis(coeff=0.97, input_name='raw'),
    # ====== STFT ====== #
    pp.speech.STFTExtractor(frame_length=Config.FRAME_LENGTH,
                            step_length=Config.STEP_LENGTH,
                            n_fft=Config.NFFT, window=Config.WINDOW),
    pp.base.RenameFeatures(input_name='stft_energy', output_name='energy'),
    # ====== SAD ====== #
    pp.speech.SADextractor(nb_mixture=3, nb_train_it=25,
                           input_name='energy', output_name='sad'),
    # ====== for x-vector ====== #
    pp.speech.PowerSpecExtractor(power=2.0, input_name='stft', output_name='spec'),
    pp.speech.MelsSpecExtractor(n_mels=Config.NMELS,
                                fmin=Config.FMIN, fmax=Config.FMAX,
                                input_name=('spec', 'sr'), output_name='mspec'),
    pp.speech.ApplyingSAD(input_name='mspec', sad_name='sad'),
    # ====== normalization ====== #
    pp.speech.AcousticNorm(mean_var_norm=True, windowed_mean_var_norm=True,
                           win_length=301, input_name='mspec'),
    # ====== post processing ====== #
    pp.base.DeleteFeatures(input_name=['stft', 'spec', 'raw',
                                       'sad', 'energy', 'sad_threshold']),
    pp.base.AsType(dtype='float16'),
], debug=IS_DEBUGGING)
# ===========================================================================
# Running the extractor
# ===========================================================================
with np.warnings.catch_warnings():
  np.warnings.filterwarnings('ignore')
  # ====== debugging ====== #
  if IS_DEBUGGING:
    perm = np.random.permutation(len(ALL_FILES))
    for row in ALL_FILES[perm][:18]:
      feat = extractors.transform(row)
      feat['mspec'] = feat['mspec'][:800]
      V.plot_multiple_features(feat, title=feat['path'])
    V.plot_save(os.path.join(EXP_DIR, 'features_debug.pdf'))
  # ====== main processor ====== #
  else:
    processor = pp.FeatureProcessor(jobs=ALL_FILES, path=PATH_ACOUSTIC_FEATURES,
                                    extractor=extractors,
                                    n_cache=250, ncpu=None, override=True,
                                    identifier='name',
                                    log_path=os.path.join(EXP_DIR, 'processor.log'))
    with UnitTimer():
      processor.run()
# ===========================================================================
# Make some visualization
# ===========================================================================
if os.path.exists(PATH_ACOUSTIC_FEATURES):
  ds = F.Dataset(PATH_ACOUSTIC_FEATURES, read_only=True)
  print(ds)
  V.plot_figure(nrow=7, ncol=8)
  for idx, (name, (start, end)) in enumerate(sampling_iter(ds['indices'].items(),
                                                           k=12,
                                                           seed=52181208)):
    X = ds['mspec'][start:end][:800, :].astype('float32')
    dataset = ds['ds'][name]
    V.plot_spectrogram(X.T,
                       ax =(6, 2, idx + 1),
                       title='%s-%s' % (name, dataset))
  V.plot_save(os.path.join(EXP_DIR, 'features_extracted.pdf'))
  ds.close()