#!/usr/bin/env python3

import numpy as np
import math
import pprint
import torch
import torch.nn as nn
import torch.nn.functional as F
from torch.autograd import Variable
import torchvision.models
import math
import copy
from torch.nn.init import kaiming_normal, kaiming_uniform, xavier_uniform, xavier_normal, constant

from vr.models.layers import build_classifier, build_stem
import vr.programs

class TMAC(nn.Module):
  """Implementation of the Compositional Attention Networks from: https://openreview.net/pdf?id=S1Euwz-Rb"""
  def __init__(self, vocab, feature_dim,
               stem_num_layers,
               stem_batchnorm,
               stem_kernel_size,
               stem_subsample_layers,
               stem_stride,
               stem_padding,
               children_list,
               module_dim,
               question_embedding_dropout,
               read_dropout,
               use_prior_control_in_control_unit,
               sharing_params_patterns,
               classifier_fc_layers,
               classifier_batchnorm,
               classifier_dropout,
               use_coords,
               debug_every=float('inf'),
               print_verbose_every=float('inf'),
               verbose=True,
               ):
    super(TMAC, self).__init__()

    num_answers = len(vocab['answer_idx_to_token'])

    self.stem_times = []
    self.module_times = []
    self.classifier_times = []
    self.timing = False

    self.children_list = children_list

    self.question_embedding_dropout = question_embedding_dropout
    #self.memory_dropout = memory_dropout
    self.read_dropout = read_dropout

    self.module_dim = module_dim

    self.sharing_params_patterns = [True if p == 1 else False for p in sharing_params_patterns]

    self.use_coords_freq = use_coords
    self.debug_every = debug_every
    self.print_verbose_every = print_verbose_every

    # Initialize helper variables
    self.stem_use_coords = self.use_coords_freq
    self.extra_channel_freq = self.use_coords_freq

    self.fwd_count = 0
    self.num_extra_channels = 2 if self.use_coords_freq > 0 else 0
    if self.debug_every <= -1:
      self.print_verbose_every = 1

    # Initialize stem
    stem_feature_dim = feature_dim[0] + self.stem_use_coords * self.num_extra_channels
    self.stem = build_stem(stem_feature_dim, module_dim,
                           num_layers=stem_num_layers, with_batchnorm=stem_batchnorm,
                           kernel_size=stem_kernel_size, stride=stem_stride, padding=stem_padding,
                           subsample_layers=stem_subsample_layers, acceptEvenKernel=True)


    #Define units
    unique_children_number = set(map(len, self.children_list))
    
    if self.sharing_params_patterns[0]:
      mod = InputUnit(module_dim)
      self.add_module('InputUnit', mod)
      self.InputUnits = mod
    else:
      self.InputUnits = []
      for i in range(len(self.children_list)):
        mod = InputUnit(module_dim)
        self.add_module('InputUnit' + str(i), mod)
        self.InputUnits.append(mod)

    if self.sharing_params_patterns[1]:
      self.ControlUnits = {}
      for num in unique_children_number:
        mod = ControlUnit(num, module_dim, use_prior_control_in_control_unit=use_prior_control_in_control_unit)
        self.add_module('ControlUnit' + str(num), mod)
        self.ControlUnits[num] = mod
    else:
      self.ControlUnits = []
      for i, children in enumerate(self.children_list):
        mod = ControlUnit(len(children), module_dim, use_prior_control_in_control_unit=use_prior_control_in_control_unit)
        self.add_module('ControlUnit' + str(i), mod)
        self.ControlUnits.append(mod)

    if self.sharing_params_patterns[2]:
      self.ReadUnits = {}
      for num in unique_children_number:
        mod = ReadUnit(num, module_dim, 'ReadUnit' + str(num), self.read_dropout)
        self.add_module('ReadUnit' + str(num), mod)
        self.ReadUnits[num] = mod
    else:
      self.ReadUnits = []
      for i, children in enumerate(self.children_list):
        mod = ReadUnit(len(children), module_dim, 'ReadUnit' + str(i), self.read_dropout)
        self.add_module('ReadUnit' + str(i), mod)
        self.ReadUnits.append(mod)

    if self.sharing_params_patterns[3]:
      self.WriteUnits = {}
      for num in unique_children_number:
        mod = WriteUnit(num, module_dim)
        self.add_module('WriteUnit' + str(num), mod)
        self.WriteUnits[num] = mod
    else:
      self.WriteUnits = []
      for i, children in enumerate(self.children_list):
        mod = WriteUnit(len(children), module_dim)
        self.add_module('WriteUnit' + str(i), mod)
        self.WriteUnits.append(mod)

    #parameters for initial memory and control vectors
    self.init_memory = nn.Parameter(torch.randn(module_dim).cuda())

    #first transformation of question embeddings
    self.init_question_transformer = nn.Linear(self.module_dim, self.module_dim)
    self.init_question_non_linear = nn.Tanh()

    self.vocab = vocab

    self.question_embedding_dropout_module = nn.Dropout(p=self.question_embedding_dropout)

    # Initialize output classifier
    self.classifier = OutputUnit(module_dim, classifier_fc_layers, num_answers,
                                 with_batchnorm=classifier_batchnorm, dropout=classifier_dropout)

    init_modules(self.modules())

  def forward(self, x, ques, save_activations=False):
    # Initialize forward pass and externally viewable activations
    self.fwd_count += 1
    if save_activations:
      self.feats = None
      self.control_outputs = []
      self.memory_outputs = []
      self.cf_input = None

    q_context, q_rep, q_mask = ques

    original_q_rep = q_rep

    q_rep = self.question_embedding_dropout_module(q_rep)

    init_control = q_rep

    q_rep = self.init_question_non_linear(self.init_question_transformer(q_rep))

    stem_batch_coords = None
    if self.use_coords_freq > 0:
      stem_coords = coord_map((x.size(2), x.size(3)))
      stem_batch_coords = stem_coords.unsqueeze(0).expand(
          torch.Size((x.size(0), *stem_coords.size())))
    if self.stem_use_coords:
      x = torch.cat([x, stem_batch_coords], 1)
    feats = self.stem(x)
    if save_activations:
      self.feats = feats
    N, _, H, W = feats.size()

    control_storage = Variable(torch.zeros(N, 1+len(self.children_list), self.module_dim)).type(torch.cuda.FloatTensor)
    memory_storage = Variable(torch.zeros(N, 1+len(self.children_list), self.module_dim)).type(torch.cuda.FloatTensor)

    control_storage[:,0,:] = init_control
    memory_storage[:,0,:] = self.init_memory.expand(N, self.module_dim)

    for nidx in range(len(self.children_list)-1,-1,-1):
      children = self.children_list[nidx]
      num_children = len(children)
      
      inputUnit = self.InputUnits[nidx] if isinstance(self.InputUnits, list) else self.InputUnits
      
      if self.sharing_params_patterns[1]:
        controlUnit = self.ControlUnits[num_children]
      else:
        controlUnit = self.ControlUnits[nidx]
      
      if self.sharing_params_patterns[2]:
        readUnit = self.ReadUnits[num_children]
      else:
        readUnit = self.ReadUnits[nidx]
      
      if self.sharing_params_patterns[3]:
        writeUnit = self.WriteUnits[num_children]
      else:
        writeUnit = self.WriteUnits[nidx]

      #compute question representation specific to this cell
      q_rep_i = inputUnit(q_rep) # N x d

      #compute control at the current step
      pre_controls = []
      if num_children == 0:
        pre_controls.append(control_storage[:,0,:])
      else:
        for child in children:
          pre_controls.append(control_storage[:,child+1,:])
      control_i = controlUnit(pre_controls, q_rep_i, q_context, q_mask)
      if save_activations:
        self.control_outputs.append(control_i)
      control_updated = control_storage.clone()
      control_updated[:,(nidx+1),:] = control_updated[:,(nidx+1),:] + control_i
      control_storage = control_updated

      #compute read at the current step
      pre_memories = []
      if num_children == 0:
        pre_memories.append(memory_storage[:,0,:])
      else:
        for child in children:
          pre_memories.append(memory_storage[:,child+1,:])
      read_i = readUnit(pre_memories, control_updated[:,(nidx+1),:], feats)

      #compute write memeory at the current step
      memory_i = writeUnit(memory_storage, read_i, children)

      if save_activations:
        self.memory_outputs.append(memory_i)

      if nidx == 0:
        final_module_output = memory_i
      else:
        memory_updated = memory_storage.clone()
        memory_updated[:,(nidx+1),:] = memory_updated[:,(nidx+1),:] + memory_i
        memory_storage = memory_updated

    if save_activations:
      self.cf_input = final_module_output

    out = self.classifier(final_module_output, original_q_rep)

    return out

class OutputUnit(nn.Module):
  def __init__(self, module_dim, hidden_units, num_outputs, with_batchnorm=False, dropout=0.0):
    super(OutputUnit, self).__init__()

    self.dropout = dropout

    self.question_transformer = nn.Linear(module_dim, module_dim)

    input_dim = 2*module_dim
    hidden_units = [input_dim] + [h for h in hidden_units] + [num_outputs]
    self.linears = []
    self.batchnorms = []
    for i, (nin, nout) in enumerate(zip(hidden_units, hidden_units[1:])):
      mod = nn.Linear(nin, nout)
      self.add_module('MAC_LinearFC' + str(i), mod)
      self.linears.append(mod)
      mod = nn.BatchNorm1d(nin) if with_batchnorm else None
      if mod is not None: self.add_module('MAC_BatchNormFC' + str(i), mod)
      self.batchnorms.append(mod)

    self.non_linear = nn.ReLU()
    self.dropout_module = nn.Dropout(p=self.dropout)

    init_modules(self.modules())

  def forward(self, final_memory, original_q_rep):

    transformed_question = self.question_transformer(original_q_rep)
    features = torch.cat([final_memory, transformed_question], 1)

    for i, (linear, batchnorm) in enumerate(zip(self.linears, self.batchnorms)):
      if batchnorm is not None:
        features = batchnorm(features)
      features = self.dropout_module(features)
      features = linear(features)
      if i < len(self.linears) - 1:
        features = self.non_linear(features)

    return features

class WriteUnit(nn.Module):
  def __init__(self, num_children, common_dim):
    super(WriteUnit, self).__init__()
    self.num_children = num_children
    if num_children == 0: self.num_children += 1
    self.common_dim = common_dim
    
    self.control_memory_transfomer = nn.Linear((self.num_children+1) * common_dim, common_dim) #Eq (w1)

    init_modules(self.modules())

  def forward(self, memories, current_read, _idx):
    #memories (N x num_cell x d), controls (N x num_cell x d), current_read (N x d), idx (int starting from 1)
    
    idx = copy.deepcopy(_idx)
    if len(idx) == 0: idx = [-1]
    assert len(idx) == self.num_children
    prior_memories = []
    for i in idx:
      prior_memories.append(memories[:,i+1,:])
    
    res_memory = self.control_memory_transfomer( torch.cat(prior_memories + [current_read], 1) ) #N x d

    return res_memory


class ReadUnit(nn.Module):
  def __init__(self, num_children, common_dim, prefix='', read_dropout=0.):
    super(ReadUnit, self).__init__()
    self.num_children = num_children
    if num_children == 0: self.num_children += 1
    self.common_dim = common_dim
    self.read_dropout = read_dropout

    #Eq (r1)
    self.image_element_transformer = nn.Linear(common_dim, common_dim)
    self.pre_memory_transformers = []
    for i in range(self.num_children):
      mod = nn.Linear(common_dim, common_dim)
      self.add_module(prefix + '_pre_memory_trans_' + str(i), mod)
      self.pre_memory_transformers.append(mod)

    #Eq (r2)
    self.intermediate_transformer = nn.Linear((self.num_children+1) * common_dim, common_dim)

    #Eq (r3.1)
    self.read_attention_transformer = nn.Linear(common_dim, 1)

    self.non_linear = nn.ReLU()
    self.read_dropout_module = nn.Dropout(p=self.read_dropout)

    init_modules(self.modules())

  def forward(self, pre_memories, current_control, image):

    #pre_memory(Nxd), current_control(Nxd), image(NxdxHxW)

    image = image.transpose(1,2).transpose(2,3) #NXHxWxd
    trans_image = image
    
    trans_image = self.read_dropout_module(trans_image)
    for i in range(len(pre_memories)):
      pre_memories[i] = self.read_dropout_module(pre_memories[i])

    #Eq (r1)
    intermediates = []
    trans_image = self.image_element_transformer(trans_image) #NxHxWxd image
    assert len(pre_memories) == len(self.pre_memory_transformers)
    for i in range(len(pre_memories)):
      pre_memories[i] = self.pre_memory_transformers[i](pre_memories[i]) #Nxd
      pre_memories[i] = pre_memories[i].unsqueeze(1).unsqueeze(2).expand(trans_image.size()) #NxHxWxd
      intermediates.append(pre_memories[i] * trans_image) #NxHxWxd

    #Eq (r2)
    #trans_intermediate = self.intermediate_transformer(torch.cat([intermediate, image], 3)) #NxHxWxd
    trans_intermediate = self.intermediate_transformer(torch.cat(intermediates + [trans_image], 3)) #NxHxWxd
    trans_intermediate = self.non_linear(trans_intermediate)

    #Eq (r3.1)
    trans_current_control = current_control.unsqueeze(1).unsqueeze(2).expand(trans_intermediate.size()) #NxHxWxd
    intermediate_score = trans_current_control * trans_intermediate

    intermediate_score = self.non_linear(intermediate_score)

    intermediate_score = self.read_dropout_module(intermediate_score)

    scores = self.read_attention_transformer(intermediate_score).squeeze(3) #NxHxWx1 -> NxHxW

    #Eq (r3.2): softmax
    rscores = scores.view(scores.shape[0], -1) #N x (H*W)
    rscores = torch.exp(rscores - rscores.max(1, keepdim=True)[0])
    rscores = rscores / rscores.sum(1, keepdim=True)
    scores = rscores.view(scores.shape) #NxHxW

    #Eq (r3.3)
    readrep = image * scores.unsqueeze(3)
    readrep = readrep.view(readrep.shape[0], -1, readrep.shape[-1]) #N x (H*W) x d
    readrep = readrep.sum(1) #N x d

    return readrep

class ControlUnit(nn.Module):
  def __init__(self, num_children, common_dim, use_prior_control_in_control_unit=False):
    super(ControlUnit, self).__init__()
    self.num_children = num_children
    if num_children == 0: self.num_children += 1
    self.common_dim = common_dim
    self.use_prior_control_in_control_unit = use_prior_control_in_control_unit

    if use_prior_control_in_control_unit:
      self.control_question_transformer = nn.Linear((self.num_children+1) * common_dim, common_dim) #Eq (c1)

    self.score_transformer = nn.Linear(common_dim, 1) # Eq (c2.1)

    init_modules(self.modules())

  def forward(self, pre_controls, question, context, mask):

    #pre_control (Nxd), question (Nxd), context(NxLxd), mask(NxL)
    
    assert len(pre_controls) == self.num_children

    #Eq (c1)
    if self.use_prior_control_in_control_unit:
      control_question = self.control_question_transformer(torch.cat(pre_controls + [question], 1)) # N x d
    else:
      control_question = question # N x d

    #Eq (c2.1)
    scores = self.score_transformer(context * control_question.unsqueeze(1)).squeeze(2)  #NxLxd -> NxLx1 -> NxL

    #Eq (c2.2) : softmax
    scores = torch.exp(scores - scores.max(1, keepdim=True)[0]) * mask #mask help to eliminate null tokens
    scores = scores / scores.sum(1, keepdim=True) #NxL

    #Eq (c2.3)
    control = (context * scores.unsqueeze(2)).sum(1) #Nxd

    return control

class InputUnit(nn.Module):
  def __init__(self, common_dim):
    super(InputUnit, self).__init__()
    self.common_dim = common_dim
    self.question_transformer = nn.Linear(common_dim, common_dim)

    init_modules(self.modules())

  def forward(self, question):
    return self.question_transformer(question) #Section 2.1

def coord_map(shape, start=-1, end=1):
  """
  Gives, a 2d shape tuple, returns two mxn coordinate maps,
  Ranging min-max in the x and y directions, respectively.
  """
  m, n = shape
  x_coord_row = torch.linspace(start, end, steps=n).type(torch.cuda.FloatTensor)
  y_coord_row = torch.linspace(start, end, steps=m).type(torch.cuda.FloatTensor)
  x_coords = x_coord_row.unsqueeze(0).expand(torch.Size((m, n))).unsqueeze(0)
  y_coords = y_coord_row.unsqueeze(1).expand(torch.Size((m, n))).unsqueeze(0)
  return Variable(torch.cat([x_coords, y_coords], 0))

def sincos_coord_map(shape, p_h=64., p_w=64.):
  m, n = shape
  x_coords = torch.zeros(m,n)
  y_coords = torch.zeros(m,n)

  for i in range(m):
    for j in range(n):
      icoord = i if i % 2 == 0 else i-1
      jcoord = j if j % 2 == 0 else j-1
      x_coords[i, j] = math.sin(1.0 * i / (10000. ** (1.0 * jcoord / p_h)))
      y_coords[i, j] = math.cos(1.0 * j / (10000. ** (1.0 * icoord / p_w)))

  x_coords = x_coords.type(torch.cuda.FloatTensor).unsqueeze(0)
  y_coords = y_coords.type(torch.cuda.FloatTensor).unsqueeze(0)

  return Variable(torch.cat([x_coords, y_coords], 0))


def init_modules(modules, init='uniform'):
  if init.lower() == 'normal':
    init_params = xavier_normal
  elif init.lower() == 'uniform':
    init_params = xavier_uniform
  else:
    return
  for m in modules:
    if isinstance(m, (nn.Conv2d, nn.Linear)):
      init_params(m.weight)
      if m.bias is not None: constant(m.bias, 0.)