# Copyright 2016 The TensorFlow Authors. All Rights Reserved.
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
# ==============================================================================

from collections import namedtuple
import numpy as np
import tensorflow as tf
from tensorflow.contrib.cudnn_rnn.python.ops import cudnn_rnn_ops
import lib

# Import pythonrouge package
from pythonrouge import PythonROUGE
ROUGE_dir = "/qydata/ywubw/download/RELEASE-1.5.5"
sentence_sep = "</s>"

# NB: batch_size could be unspecified (None) in decode mode
HParams = namedtuple("HParams", "mode, min_lr, lr, dropout, batch_size,"
                     "num_sentences, num_words_sent, rel_pos_max_idx,"
                     "enc_layers, enc_num_hidden, emb_dim, pos_emb_dim,"
                     "doc_repr_dim, word_conv_k_sizes, word_conv_filter,"
                     "min_num_input_sents, min_num_words_sent,"
                     "max_grad_norm, decay_step, decay_rate,"
                     "trg_weight_norm, train_mode, mlp_num_hidden")

rouge = PythonROUGE(
    ROUGE_dir,
    n_gram=2,
    ROUGE_SU4=False,
    ROUGE_L=False,
    stemming=True,
    stopwords=False,
    length_limit=False,
    length=75,
    word_level=False,
    use_cf=True,
    cf=95,
    ROUGE_W=False,
    ROUGE_W_Weight=1.2,
    scoring_formula="average",
    resampling=False,
    samples=1000,
    favor=False,
    p=0.5)


def CreateHParams(flags):
  """Create Hyper-parameters from tf.app.flags.FLAGS"""

  word_conv_k_sizes = [str(n) for n in flags.word_conv_k_sizes.split(",")]
  assert flags.mode in ["train", "decode"], "Invalid mode."
  assert flags.train_mode in ["sl", "rl"], "Invalid train_mode."

  hps = HParams(
      mode=flags.mode,  # train, eval, decode
      train_mode=flags.train_mode,  # sl, rl
      lr=flags.lr,
      min_lr=flags.min_lr,
      dropout=flags.dropout,
      batch_size=flags.batch_size,
      num_sentences=flags.num_sentences,  # number of sentences in a document
      num_words_sent=flags.num_words_sent,  # number of words in a sentence
      rel_pos_max_idx=flags.rel_pos_max_idx,  # number of relative positions
      enc_layers=flags.enc_layers,  # number of layers for sentence-level rnn
      enc_num_hidden=flags.enc_num_hidden,  # for sentence-level rnn
      emb_dim=flags.emb_dim,
      pos_emb_dim=flags.pos_emb_dim,
      doc_repr_dim=flags.doc_repr_dim,
      word_conv_k_sizes=word_conv_k_sizes,
      word_conv_filter=flags.word_conv_filter,
      mlp_num_hidden=lib.parse_list_str(flags.mlp_num_hidden),
      min_num_input_sents=flags.min_num_input_sents,  # for batch reader
      min_num_words_sent=flags.min_num_words_sent,  # for batch reader
      max_grad_norm=flags.max_grad_norm,
      decay_step=flags.decay_step,
      decay_rate=flags.decay_rate,
      trg_weight_norm=flags.trg_weight_norm)
  return hps


def TrainLoop(model, sess, batcher, valid_batcher, summary_writer, flags):
  """Runs model training."""
  step, losses = 0, []
  while step < flags.max_run_steps:
    next_batch = batcher.next()
    summaries, loss, train_step = model.run_train_step(sess, next_batch)
    losses.append(loss)
    summary_writer.add_summary(summaries, train_step)
    step += 1

    # Display current training loss
    if step % flags.display_freq == 0:
      avg_loss = lib.compute_avg(losses, summary_writer, "avg_loss", train_step)
      tf.logging.info("Train step %d: avg_loss %f" % (train_step, avg_loss))
      losses = []
      summary_writer.flush()

    # Run evaluation on validation set
    if step % flags.valid_freq == 0:
      model.run_valid_steps(sess, valid_batcher, flags.num_valid_batch,
                            summary_writer)
      summary_writer.flush()


class SummaRuNNerRF(object):
  """ Implements extractive summarization model based on the following works:

  [1] Cheng, J., & Lapata, M. (2016). Neural Summarization by Extracting Sentences
  and Words. arXiv:1603.07252 [Cs]. Retrieved from http://arxiv.org/abs/1603.07252
  [2] Nallapati, R., Zhai, F., & Zhou, B. (2016). SummaRuNNer: A Recurrent Neural
  Network based Sequence Model for Extractive Summarization of Documents.
  arXiv:1611.04230 [Cs]. Retrieved from http://arxiv.org/abs/1611.04230

  This is the extractive version of SummaRuNNer.
  """

  def __init__(self, hps, input_vocab, num_gpus=0):
    if hps.mode not in ["train", "decode"]:
      raise ValueError("Only train and decode mode are supported.")

    self._hps = hps
    self._input_vocab = input_vocab
    self._num_gpus = num_gpus

  def build_graph(self):
    self._allocate_devices()
    self._add_placeholders()
    self._build_model()
    self.global_step = tf.Variable(0, name="global_step", trainable=False)

    if self._hps.mode == "train":
      self._add_loss()
      self._add_train_op()

    self._summaries = tf.summary.merge_all()

  def _allocate_devices(self):
    num_gpus = self._num_gpus
    assert num_gpus >= 0

    if num_gpus == 0:
      raise ValueError("Current implementation requires at least one GPU.")
    elif num_gpus == 1:
      self._device_0 = "/gpu:0"
      self._device_1 = "/gpu:0"
    elif num_gpus > 1:
      self._device_0 = "/gpu:0"
      self._device_1 = "/gpu:0"
      tf.logging.warn("Current implementation uses at most one GPU.")

  def _add_placeholders(self):
    """Inputs to be fed to the graph."""
    hps = self._hps
    # Input sequence
    self._inputs = tf.placeholder(
        tf.int32, [hps.batch_size, hps.num_sentences, hps.num_words_sent],
        name="inputs")
    self._input_sent_lens = tf.placeholder(
        tf.int32, [hps.batch_size, hps.num_sentences], name="input_sent_lens")
    self._input_doc_lens = tf.placeholder(
        tf.int32, [hps.batch_size], name="input_doc_lens")
    self._input_rel_pos = tf.placeholder(
        tf.int32, [hps.batch_size, hps.num_sentences], name="input_rel_pos")

    # Output extraction decisions
    self._extract_targets = tf.placeholder(
        tf.int32, [hps.batch_size, hps.num_sentences], name="extract_targets")
    # May weight the extraction decisions differently
    self._target_weights = tf.placeholder(
        tf.float32, [hps.batch_size, hps.num_sentences], name="target_weights")

    # For RL mode
    self._document_strs = tf.placeholder(
        tf.string, [hps.batch_size], name="document_strs")
    self._summary_strs = tf.placeholder(
        tf.string, [hps.batch_size], name="summary_strs")

  def _build_model(self):
    """Construct the deep neural network of SummaRuNNer."""
    hps = self._hps

    batch_size_ts = hps.batch_size  # batch size Tensor
    if hps.batch_size is None:
      batch_size_ts = tf.shape(self._inputs)[0]

    with tf.variable_scope("summarunner"):
      with tf.variable_scope("embeddings"):
        self._add_embeddings()

      # Encoder
      with tf.variable_scope("encoder",
          initializer=tf.random_uniform_initializer(-0.1, 0.1)), \
          tf.device(self._device_0):
        # Hierarchical encoding of input document
        self._sentence_vecs = self._add_encoder(
            self._inputs, self._input_sent_lens, self._input_doc_lens
        )  # [num_sentences, batch_size, enc_num_hidden*2]
        # Note: output size of Bi-RNN is double of the enc_num_hidden

        # Document representation
        doc_mean_vec = tf.div(
            tf.reduce_sum(self._sentence_vecs, 0),
            tf.expand_dims(tf.to_float(self._input_doc_lens),
                           1))  # [batch_size, enc_num_hidden*2]
        self._doc_repr = tf.tanh(
            lib.linear(
                doc_mean_vec, hps.doc_repr_dim, True, scope="doc_repr_linear"))

        # Absolute position embedding
        abs_pos_idx = tf.expand_dims(tf.range(0, hps.num_sentences),
                                     1)  # [num_sentences, 1]

        abs_pos_batch = tf.tile(abs_pos_idx, tf.stack(
            [1, batch_size_ts]))  # [num_sentences, batch_size]
        self._sent_abs_pos_emb = tf.nn.embedding_lookup(
            self._abs_pos_embed,
            abs_pos_batch)  # [num_sentences, batch_size, pos_emb_dim]

        # Relative position embedding
        self._sent_rel_pos_emb = tf.nn.embedding_lookup(
            self._rel_pos_embed,
            self._input_rel_pos)  # [batch_size, num_sentences, pos_emb_dim]

        # Unstack the features into list: num_sentences * [batch_size, ?]
        sentence_vecs_list = tf.unstack(self._sentence_vecs, axis=0)
        abs_pos_emb_list = tf.unstack(self._sent_abs_pos_emb, axis=0)
        rel_pos_emb_list = tf.unstack(self._sent_rel_pos_emb, axis=1)

      # Compute the extraction probability of each sentence
      with tf.variable_scope("extract_sent",
          initializer=tf.random_uniform_initializer(-0.1, 0.1)), \
          tf.device(self._device_0):

        if hps.mode == "train":  # train mode
          # Initialize the representation of all historical summaries extracted
          hist_summary = tf.zeros_like(sentence_vecs_list[0])
          extract_logit_list, extract_prob_list = [], []

          if hps.train_mode == "sl":
            targets = tf.unstack(
                tf.to_float(self._extract_targets),
                axis=1)  # [batch_size] * num_sentences
          else:
            sampled_target_list = []

          for i in xrange(hps.num_sentences):
            cur_sent_vec = sentence_vecs_list[i]
            cur_abs_pos = abs_pos_emb_list[i]
            cur_rel_pos = rel_pos_emb_list[i]

            if i > 0:
              tf.get_variable_scope().reuse_variables()  # NB: this is important!

            extract_logit = self._compute_extract_prob(
                cur_sent_vec, cur_abs_pos, cur_rel_pos, self._doc_repr,
                hist_summary)  # [batch_size, 2]
            extract_logit_list.append(extract_logit)
            extract_prob = tf.nn.softmax(extract_logit)  # [batch_size, 2]
            extract_prob_list.append(extract_prob)

            if hps.train_mode == "sl":
              target = tf.expand_dims(targets[i], 1)  # [batch_size, 1] float32
              hist_summary += target * cur_sent_vec  #[batch_size, enc_num_hidden*2]
            else:
              sampled_target = tf.multinomial(
                  logits=extract_logit, num_samples=1)  # [batch_size, 1] int32
              sampled_target_list.append(sampled_target)
              hist_summary += tf.to_float(
                  sampled_target
              ) * cur_sent_vec  # [batch_size, enc_num_hidden*2]

          self._extract_logits = tf.stack(
              extract_logit_list, axis=1)  # [batch_size, num_sentences, 2]
          self._extract_probs = tf.stack(
              extract_prob_list, axis=1)  # [batch_size, num_sentences, 2]

          if hps.train_mode == "rl":
            self._sampled_targets = tf.concat(
                sampled_target_list,
                axis=1)  # [batch_size, num_sentences] int32

        else:  # decode mode
          self._cur_sent_vec = tf.placeholder(tf.float32,
                                              sentence_vecs_list[0].get_shape())
          self._cur_abs_pos = tf.placeholder(tf.float32,
                                             abs_pos_emb_list[0].get_shape())
          self._cur_rel_pos = tf.placeholder(tf.float32,
                                             rel_pos_emb_list[0].get_shape())
          self._hist_summary = tf.placeholder(tf.float32,
                                              sentence_vecs_list[0].get_shape())

          extract_logit = self._compute_extract_prob(
              self._cur_sent_vec, self._cur_abs_pos, self._cur_rel_pos,
              self._doc_repr, self._hist_summary)  # [batch_size, 2]
          self._ext_log_prob = tf.log(
              tf.nn.softmax(extract_logit))  # [batch_size, 2]

  def _add_embeddings(self):
    hps = self._hps
    input_vsize = self._input_vocab.NumIds

    with tf.device(self._device_0):
      # Input word embeddings
      self._input_embed = tf.get_variable(
          "input_embed", [input_vsize, hps.emb_dim],
          dtype=tf.float32,
          initializer=tf.truncated_normal_initializer(stddev=1e-4))
      # Absolute position embeddings
      self._abs_pos_embed = tf.get_variable(
          "abs_pos_embed", [hps.num_sentences, hps.pos_emb_dim],
          dtype=tf.float32,
          initializer=tf.truncated_normal_initializer(stddev=1e-4))
      # Relative position embeddings
      self._rel_pos_embed = tf.get_variable(
          "rel_pos_embed", [hps.rel_pos_max_idx, hps.pos_emb_dim],
          dtype=tf.float32,
          initializer=tf.truncated_normal_initializer(stddev=1e-4))

  def _add_encoder(self, inputs, sent_lens, doc_lens, transpose_output=False):
    hps = self._hps

    # Masking the word embeddings
    sent_lens_rsp = tf.reshape(sent_lens, [-1])  # [batch_size * num_sentences]
    word_masks = tf.expand_dims(
        tf.sequence_mask(
            sent_lens_rsp, maxlen=hps.num_words_sent, dtype=tf.float32),
        2)  # [batch_size * num_sentences, num_words_sent, 1]

    inputs_rsp = tf.reshape(inputs, [-1, hps.num_words_sent])
    emb_inputs = tf.nn.embedding_lookup(
        self._input_embed,
        inputs_rsp)  # [batch_size * num_sentences, num_words_sent, emb_size]
    emb_inputs = emb_inputs * word_masks

    # Level 1: Add the word-level convolutional neural network
    word_conv_outputs = []
    for k_size in hps.word_conv_k_sizes:
      # Create CNNs with different kernel width
      word_conv_k = tf.layers.conv1d(
          emb_inputs,
          hps.word_conv_filter, (k_size,),
          padding="same",
          kernel_initializer=tf.random_uniform_initializer(-0.1, 0.1))
      mean_pool_sent = tf.reduce_mean(
          word_conv_k, axis=1)  # [batch_size * num_sentences, word_conv_filter]
      word_conv_outputs.append(mean_pool_sent)

    word_conv_output = tf.concat(
        word_conv_outputs, axis=1)  # concat the sentence representations
    # Reshape the representations of sentences
    sentence_size = len(hps.word_conv_k_sizes) * hps.word_conv_filter
    sentence_repr = tf.reshape(word_conv_output, [
        -1, hps.num_sentences, sentence_size
    ])  # [batch_size, num_sentences, sentence_size]

    # Level 2: Add the sentence-level RNN
    enc_model = cudnn_rnn_ops.CudnnGRU(
        hps.enc_layers,
        hps.enc_num_hidden,
        sentence_size,
        direction="bidirectional",
        dropout=hps.dropout)
    # Compute the total size of RNN params (Tensor)
    params_size_ts = enc_model.params_size()
    params = tf.Variable(
        tf.random_uniform([params_size_ts], minval=-0.1, maxval=0.1),
        validate_shape=False,
        name="encoder_cudnn_gru_var")

    batch_size_ts = tf.shape(inputs)[0]  # batch size Tensor
    init_state = tf.zeros(tf.stack([2, batch_size_ts, hps.enc_num_hidden]))
    # init_c = tf.zeros(tf.stack([2, batch_size_ts, hps.enc_num_hidden]))

    # Call the CudnnGRU
    sentence_vecs_t = tf.transpose(sentence_repr, [1, 0, 2])
    sent_rnn_output, _ = enc_model(
        input_data=sentence_vecs_t, input_h=init_state,
        params=params)  # [num_sentences, batch_size, enc_num_hidden*2]

    # Masking the paddings
    sent_out_masks = tf.sequence_mask(doc_lens, hps.num_sentences,
                                      tf.float32)  # [batch_size, num_sentences]
    sent_out_masks = tf.expand_dims(tf.transpose(sent_out_masks),
                                    2)  # [num_sentences, batch_size, 1]
    sent_rnn_output = sent_rnn_output * sent_out_masks  # [num_sentences, batch_size, enc_num_hidden*2]

    if transpose_output:
      sent_rnn_output = tf.transpose(
          sent_rnn_output, [1, 0,
                            2])  # [batch_size, num_sentences, enc_num_hidden*2]

    return sent_rnn_output

  def _compute_extract_prob(self, sent_vec, abs_pos_emb, rel_pos_emb, doc_repr,
                            hist_summary):
    hps = self._hps

    hist_sum_norm = tf.tanh(hist_summary)  # normalized with tanh
    mlp_hidden = tf.concat(
        [sent_vec, abs_pos_emb, rel_pos_emb, doc_repr, hist_sum_norm], axis=1)

    # Construct an MLP for extraction decisions
    for i, num_hidden in enumerate(hps.mlp_num_hidden):
      mlp_hidden = tf.contrib.layers.fully_connected(
          mlp_hidden,
          num_hidden,
          activation_fn=tf.nn.relu,  # tf.tanh/tf.sigmoid
          weights_initializer=tf.random_uniform_initializer(-0.1, 0.1),
          scope="mlp_layer_%d" % (i + 1))

    extract_logit = tf.contrib.layers.fully_connected(
        mlp_hidden,
        2,
        activation_fn=None,
        weights_initializer=tf.random_uniform_initializer(-0.1, 0.1),
        scope="mlp_output_layer")

    return extract_logit  # [batch_size, 2]

  def _add_loss(self):
    hps = self._hps

    with tf.variable_scope("loss"), tf.device(self._device_1):
      # Masking the loss
      loss_mask = tf.sequence_mask(
          self._input_doc_lens, maxlen=hps.num_sentences,
          dtype=tf.float32)  # [batch_size, num_sentences]

      if hps.train_mode == "sl":  # supervised learning
        xe_loss = tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=self._extract_targets,
            logits=self._extract_logits,
            name="sl_xe_loss")

        batch_loss = tf.div(
            tf.reduce_sum(xe_loss * self._target_weights * loss_mask, 1),
            tf.to_float(self._input_doc_lens))
      else:  # reinforcement learning
        # 1. Compute immediate rewards using a wrapped python function
        rewards, total_rewards = tf.py_func(
            self._get_rewards,
            [self._sampled_targets, self._document_strs, self._summary_strs],
            Tout=[tf.float32, tf.float32],
            stateful=False,
            name="reward_func")

        # Shape information missing in py_func output Tensors.
        # hps.batch_size must be specified when training.
        rewards.set_shape([hps.batch_size, hps.num_sentences])
        rewards_list = tf.unstack(rewards, axis=1)
        total_rewards.set_shape([hps.batch_size])
        self._avg_reward = tf.reduce_mean(total_rewards)  # average reward
        tf.summary.scalar("avg_reward", self._avg_reward)

        # 2. Compute the return value by cumulating all the advantages backwards
        rev_returns = []  # reversed list of returns
        cumulator = None
        for r in reversed(rewards_list):
          cumulator = r if cumulator is None else cumulator + r  # discount=1
          rev_returns.append(cumulator)
        returns = tf.stack(
            list(reversed(rev_returns)), axis=1)  # [batch_size, num_sentences]

        # 3. Compute the negative log-likelihood of chosen actions
        neg_log_probs = tf.nn.sparse_softmax_cross_entropy_with_logits(
            labels=self._sampled_targets,
            logits=self._extract_logits,
            name="rl_neg_log_prob")  # [batch_size, num_sentences]

        # 4. Compute the policy loss
        batch_loss = tf.div(
            tf.reduce_sum(neg_log_probs * returns * loss_mask, 1),
            tf.to_float(self._input_doc_lens))

      loss = tf.reduce_mean(batch_loss)
    tf.summary.scalar("loss", loss)
    self._loss = loss

  def _get_rewards(self, sampled_targets, doc_strs, summary_strs):
    doc_list = list(doc_str)
    summary_list = list(summary_strs)

    doc_sents_list, sum_sents_list = [], []
    for extracts, doc_str, summary_str in zip(sampled_targets, doc_list,
                                              summary_list):
      summary_sents = summary_str.split(sentence_sep)
      sum_sents_list.append(summary_sents)

      doc_sents = doc_str.split(sentence_sep)
      extract_sents = [s for e, s in zip(extracts, doc_sents) if e]
      doc_sents_list.append(extract_sents)

    #TODO: use p.map to run rouge

  def _add_train_op(self):
    """Sets self._train_op for training."""
    hps = self._hps

    self._lr_rate = tf.maximum(
        hps.min_lr,  # minimum learning rate.
        tf.train.exponential_decay(hps.lr, self.global_step, hps.decay_step,
                                   hps.decay_rate))
    tf.summary.scalar("learning_rate", self._lr_rate)

    tvars = tf.trainable_variables()
    with tf.device(self._device_1):
      # Compute gradients
      grads, global_norm = tf.clip_by_global_norm(
          tf.gradients(self._loss, tvars), hps.max_grad_norm)
      tf.summary.scalar("global_norm", global_norm)

      # Create optimizer and train ops
      optimizer = tf.train.GradientDescentOptimizer(self._lr_rate)
      self._train_op = optimizer.apply_gradients(
          zip(grads, tvars), global_step=self.global_step, name="train_step")

  def run_train_step(self, sess, batch):
    (enc_batch, enc_doc_lens, enc_sent_lens, sent_rel_pos, extract_targets,
     target_weights, _) = batch

    to_return = [self._train_op, self._summaries, self._loss, self.global_step]
    results = sess.run(
        to_return,
        feed_dict={
            self._inputs: enc_batch,
            self._input_sent_lens: enc_sent_lens,
            self._input_doc_lens: enc_doc_lens,
            self._input_rel_pos: sent_rel_pos,
            self._extract_targets: extract_targets,
            self._target_weights: target_weights
        })
    return results[1:]

  def run_eval_step(self, sess, batch):
    (enc_batch, enc_doc_lens, enc_sent_lens, sent_rel_pos, extract_targets,
     target_weights, _) = batch

    loss = sess.run(
        self._loss,
        feed_dict={
            self._inputs: enc_batch,
            self._input_sent_lens: enc_sent_lens,
            self._input_doc_lens: enc_doc_lens,
            self._input_rel_pos: sent_rel_pos,
            self._extract_targets: extract_targets,
            self._target_weights: target_weights
        })
    return loss

  def run_valid_steps(self, sess, data_batcher, num_valid_batch,
                      summary_writer):
    losses = []
    for _ in xrange(num_valid_batch):
      next_batch = data_batcher.next()
      loss = self.run_eval_step(sess, next_batch)
      losses.append(loss)

    step = self.get_global_step(sess)
    valid_loss = lib.compute_avg(losses, summary_writer, "valid_loss", step)
    tf.logging.info("\tValid step %d: avg_loss %f" % (step, valid_loss))

  def decode_get_feats(self, sess, enc_batch, enc_doc_lens, enc_sent_lens,
                       sent_rel_pos):
    """Get hidden features for decode mode."""
    if not self._hps.mode == "decode":
      raise ValueError("This method is only for decode mode.")

    to_return = [
        self._sentence_vecs, self._sent_abs_pos_emb, self._sent_rel_pos_emb,
        self._doc_repr
    ]

    results = sess.run(
        to_return,
        feed_dict={
            self._inputs: enc_batch,
            self._input_sent_lens: enc_sent_lens,
            self._input_doc_lens: enc_doc_lens,
            self._input_rel_pos: sent_rel_pos
        })
    return results

  def decode_log_probs(self, sess, sent_vec, abs_pos_embed, rel_pos_embed,
                       doc_repr, hist_summary):
    """Get log probability of extraction given a sentence and its history."""
    if not self._hps.mode == "decode":
      raise ValueError("This method is only for decode mode.")

    # sent_vec, abs_pos_embed, rel_pos_embed, doc_repr, hist_summary = features
    return sess.run(
        self._ext_log_prob,
        feed_dict={
            self._cur_sent_vec: sent_vec,
            self._cur_abs_pos: abs_pos_embed,
            self._cur_rel_pos: rel_pos_embed,
            self._doc_repr: doc_repr,
            self._hist_summary: hist_summary
        })

  def get_global_step(self, sess):
    """Get the current number of training steps."""
    return sess.run(self.global_step)
