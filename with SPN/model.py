import tensorflow as tf
import tensorflow.contrib.slim as slim
import numpy as np
from spatial_transformer import transformer
from tqdm import tqdm
from pdb import set_trace as brk

class Network(object):

	def __init__(self, sess,tf_record_file_path=None):

		self.sess = sess
		self.batch_size = 2
		self.img_height = 227
		self.img_width = 227
		self.out_height = 200
		self.out_width = 200
		self.channel = 3

		self.num_epochs = 10

		# Hyperparameters
		self.weight_detect = 1
		self.weight_landmarks = 5
		self.weight_visibility = 0.5
		self.weight_pose = 5
		self.weight_gender = 2

		#tf_Record Paramters
		self.filename_queue = tf.train.string_input_producer([tf_record_file_path], num_epochs=self.num_epochs)
		self.build_network()


	def build_network(self):

		self.X = tf.placeholder(tf.float32, [self.batch_size, self.img_height, self.img_width, self.channel], name='images')
		self.detection = tf.placeholder(tf.float32, [self.batch_size,2], name='detection')
		self.landmarks = tf.placeholder(tf.float32, [self.batch_size, 42], name='landmarks')
		self.visibility = tf.placeholder(tf.float32, [self.batch_size,21], name='visibility')
		self.pose = tf.placeholder(tf.float32, [self.batch_size,3], name='pose')
		self.gender = tf.placeholder(tf.float32, [self.batch_size,2], name='gender')

		self.X = self.load_from_tfRecord(self.filename_queue,resize_size=(self.img_width,self.img_height))
		
		theta = self.localization_network(self.X)
		T_mat = self.get_transformation_matrix(theta)
		
		cropped = transformer(self.X, T_mat, [self.out_height, self.out_width])

		net_output = self.hyperface(cropped) # (out_detection, out_landmarks, out_visibility, out_pose, out_gender)


		loss_detection = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(net_output[0], self.detection))
		
		visibility_mask = tf.reshape(tf.tile(tf.expand_dims(self.visibility, axis=2), [1,1,2]), [self.batch_size, -1])
		loss_landmarks = tf.reduce_mean(tf.square(visibility_mask*(net_output[1] - self.landmarks)))
		
		loss_visibility = tf.reduce_mean(tf.square(net_output[2] - self.visibility))
		loss_pose = tf.reduce_mean(tf.square(net_output[3] - self.pose))
		loss_gender = tf.reduce_mean(tf.nn.sigmoid_cross_entropy_with_logits(net_output[4], self.gender))

		self.loss = self.weight_detect*loss_detection + self.weight_landmarks*loss_landmarks  \
					+ self.weight_visibility*loss_visibility + self.weight_pose*loss_pose  \
					+ self.weight_gender*loss_gender



	def get_transformation_matrix(self, theta):
		with tf.name_scope('T_matrix'):
			theta = tf.expand_dims(theta, 2)
			mat = tf.constant(np.repeat(np.array([[[1,0,0],[0,0,0],[0,1,0],[0,0,0],[0,1,0],[0,0,1]]]),
										 self.batch_size, axis=0), dtype=tf.float32)
			tr_matrix = tf.squeeze(tf.matmul(mat, theta))

		return tr_matrix



	def train(self):
		
		optimizer = tf.train.AdamOptimizer().minimize(self.loss)

		writer = tf.summary.FileWriter('./logs', self.sess.graph)
		loss_summ = tf.summary.scalar('loss', self.loss)




	def hyperface(self,inputs, reuse = False):

		if reuse:
			tf.get_variable_scope().reuse_variables()

		with slim.arg_scope([slim.conv2d, slim.fully_connected],
							 activation_fn = tf.nn.relu,
							 weights_initializer = tf.truncated_normal_initializer(0.0, 0.01) ):
			
			conv1 = slim.conv2d(inputs, 96, [11,11], 4, padding= 'VALID', scope='conv1')
			max1 = slim.max_pool2d(conv1, [3,3], 2, padding= 'VALID', scope='max1')

			conv1a = slim.conv2d(max1, 256, [4,4], 4, padding= 'VALID', scope='conv1a')

			conv2 = slim.conv2d(max1, 256, [5,5], 1, scope='conv2')
			max2 = slim.max_pool2d(conv2, [3,3], 2, padding= 'VALID', scope='max2')
			conv3 = slim.conv2d(max2, 384, [3,3], 1, scope='conv3')

			conv3a = slim.conv2d(conv3, 256, [2,2], 2, padding= 'VALID', scope='conv3a')

			conv4 = slim.conv2d(conv3, 384, [3,3], 1, scope='conv4')
			conv5 = slim.conv2d(conv4, 256, [3,3], 1, scope='conv5')
			pool5 = slim.max_pool2d(conv5, [3,3], 2, padding= 'VALID', scope='pool5')

			concat_feat = tf.concat(3, [conv1a, conv3a, pool5])
			conv_all = slim.conv2d(concat_feat, 192, [1,1], 1, padding= 'VALID', scope='conv_all')
			
			shape = int(np.prod(conv_all.get_shape()[1:]))
			# transposed for weight loading from chainer model
			fc_full = slim.fully_connected(tf.reshape(tf.transpose(conv_all, [0,3,1,2]), [-1, shape]), 3072, scope='fc_full')

			fc_detection = slim.fully_connected(fc_full, 512, scope='fc_detection1')
			fc_landmarks = slim.fully_connected(fc_full, 512, scope='fc_landmarks1')
			fc_visibility = slim.fully_connected(fc_full, 512, scope='fc_visibility1')
			fc_pose = slim.fully_connected(fc_full, 512, scope='fc_pose1')
			fc_gender = slim.fully_connected(fc_full, 512, scope='fc_gender1')

			out_detection = slim.fully_connected(fc_detection, 2, scope='fc_detection2', activation_fn = None)
			out_landmarks = slim.fully_connected(fc_landmarks, 42, scope='fc_landmarks2', activation_fn = None)
			out_visibility = slim.fully_connected(fc_visibility, 21, scope='fc_visibility2', activation_fn = None)
			out_pose = slim.fully_connected(fc_pose, 3, scope='fc_pose2', activation_fn = None)
			out_gender = slim.fully_connected(fc_gender, 2, scope='fc_gender2', activation_fn = None)

		return [tf.nn.softmax(out_detection), out_landmarks, out_visibility, out_pose, tf.nn.softmax(out_gender)]



	def localization_network(self,inputs):   #VGG16

		with tf.variable_scope('localization_network'):
			with slim.arg_scope([slim.conv2d, slim.fully_connected],
								 activation_fn = tf.nn.relu,
								 weights_initializer = tf.constant_initializer(0.0)):
				
				net = slim.repeat(inputs, 2, slim.conv2d, 64, [3, 3], scope='conv1')
				net = slim.max_pool2d(net, [2, 2], scope='pool1')
				net = slim.repeat(net, 2, slim.conv2d, 128, [3, 3], scope='conv2')
				net = slim.max_pool2d(net, [2, 2], scope='pool2')
				net = slim.repeat(net, 3, slim.conv2d, 256, [3, 3], scope='conv3')
				net = slim.max_pool2d(net, [2, 2], scope='pool3')
				net = slim.repeat(net, 3, slim.conv2d, 512, [3, 3], scope='conv4')
				net = slim.max_pool2d(net, [2, 2], scope='pool4')
				net = slim.repeat(net, 3, slim.conv2d, 512, [3, 3], scope='conv5')
				net = slim.max_pool2d(net, [2, 2], scope='pool5')
				shape = int(np.prod(net.get_shape()[1:]))

				net = slim.fully_connected(tf.reshape(net, [-1, shape]), 4096, scope='fc6')
				net = slim.fully_connected(net, 1024, scope='fc7')
				net = slim.fully_connected(net, 3, biases_initializer = tf.constant_initializer(1.0) , scope='fc8')
			
		return net



	def predict(self, imgs_path):
		print 'Running inference...'
		np.set_printoptions(suppress=True)
		imgs = (np.load(imgs_path) - 127.5)/128.0
		shape = imgs.shape
		self.X = tf.placeholder(tf.float32, [shape[0], self.img_height, self.img_width, self.channel], name='images')
		pred = self.network(self.X, reuse = True)

		net_preds = self.sess.run(pred, feed_dict={self.X: imgs})

		print net_preds[-1]
		import matplotlib.pyplot as plt
		plt.imshow(imgs[-1]);plt.show()

		brk()


	def load_from_tfRecord(self,filename_queue,resize_size=None):
		
		reader = tf.TFRecordReader()
		_, serialized_example = reader.read(filename_queue)
		
		features = tf.parse_single_example(
			serialized_example,
			features={
				'image_raw':tf.FixedLenFeature([], tf.string),
				'width': tf.FixedLenFeature([], tf.int64),
				'height': tf.FixedLenFeature([], tf.int64)
			})
		
		image = tf.decode_raw(features['image_raw'], tf.float32)
		orig_height = tf.cast(features['height'], tf.int32)
		orig_width = tf.cast(features['width'], tf.int32)
		
		image_shape = tf.pack([orig_height,orig_width,3])
		image_tf = tf.reshape(image,image_shape)

		resized_image = tf.image.resize_image_with_crop_or_pad(image_tf,target_height=resize_size[1],target_width=resize_size[0])
		
		images = tf.train.shuffle_batch([resized_image],batch_size=self.batch_size,num_threads=1,capacity=50,min_after_dequeue=10)
		
		return images

	def load_weights(self, path):
		variables = slim.get_model_variables()
		print 'Loading weights...'
		for var in tqdm(variables):
			if ('conv' in var.name) and ('weights' in var.name):
				self.sess.run(var.assign(np.load(path+var.name.split('/')[0]+'/W.npy').transpose((2,3,1,0))))
			elif ('fc' in var.name) and ('weights' in var.name):
				self.sess.run(var.assign(np.load(path+var.name.split('/')[0]+'/W.npy').T))
			elif 'biases' in var.name:
				self.sess.run(var.assign(np.load(path+var.name.split('/')[0]+'/b.npy')))
		print 'Weights loaded!!'

	def print_variables(self):
		variables = slim.get_model_variables()
		print 'Model Variables:\n'
		for var in variables:
			print var.name, ' ', var.get_shape()


			

