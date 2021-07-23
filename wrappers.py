import atexit
import functools
import sys
import pickle
import threading
import traceback

import gym
import numpy as np
from PIL import Image


# wrapper
class DeepMindControl:  # 感觉是用gym和自己定义的数据结构之间的接口

    def __init__(self, name, size=(64, 64), camera=None):
        domain, task = name.split('_', 1)
        if domain == 'cup':  # Only domain with multiple words.
            domain = 'ball_in_cup'
        if isinstance(domain, str):
            from dm_control import suite
            self._env = suite.load(domain, task)
        else:
            assert task is None
            self._env = domain()
        self._size = size
        if camera is None:
            camera = dict(quadruped=2).get(domain, 0)  # Yong Lee 请问你写的什么，感觉也有点道理
        self._camera = camera

    @property
    def observation_space(self):
        spaces = {}
        for key, value in self._env.observation_spec().items():
            spaces[key] = gym.spaces.Box(
                -np.inf, np.inf, value.shape, dtype=np.float32)
        spaces['image'] = gym.spaces.Box(
            0, 255, self._size + (3,), dtype=np.uint8)
        return gym.spaces.Dict(spaces)

    @property
    def action_space(self):
        spec = self._env.action_spec()
        return gym.spaces.Box(spec.minimum, spec.maximum, dtype=np.float32)

    def step(self, action):
        time_step = self._env.step(action)
        obs = dict(time_step.observation)
        obs['image'], obs['depth'] = self.render()
        reward = time_step.reward or 0
        done = time_step.last()
        info = {'discount': np.array(time_step.discount, np.float32)}
        return obs, reward, done, info

    def reset(self):
        time_step = self._env.reset()
        obs = dict(time_step.observation)
        obs['image'], obs['depth'] = self.render()
        return obs

    def render(self, *args, **kwargs):
        if kwargs.get('mode', 'rgb_array') != 'rgb_array':
            raise ValueError("Only render mode 'rgb_array' is supported.")
        rgb = self._env.physics.render(*self._size, camera_id=self._camera)
        depth = self._env.physics.render(*self._size, camera_id=self._camera, depth=True)
        depth = depth[:, :, np.newaxis]  # 让维度和image的一致

        # cv2.imwrite("rgb.png", rgb[:, :, ::-1])
        # cv2.imwrite("depth.png", depth)
        return rgb, depth


class Atari:
    LOCK = threading.Lock()

    def __init__(
            self, name, action_repeat=4, size=(84, 84), grayscale=True, noops=30,
            life_done=False, sticky_actions=True):
        import gym
        version = 0 if sticky_actions else 4
        name = ''.join(word.title() for word in name.split('_'))
        with self.LOCK:
            self._env = gym.make('{}NoFrameskip-v{}'.format(name, version))
        self._action_repeat = action_repeat
        self._size = size
        self._grayscale = grayscale
        self._noops = noops
        self._life_done = life_done
        self._lives = None
        shape = self._env.observation_space.shape[:2] + (() if grayscale else (3,))
        self._buffers = [np.empty(shape, dtype=np.uint8) for _ in range(2)]
        self._random = np.random.RandomState(seed=None)

    @property
    def observation_space(self):
        shape = self._size + (1 if self._grayscale else 3,)
        space = gym.spaces.Box(low=0, high=255, shape=shape, dtype=np.uint8)
        return gym.spaces.Dict({'image': space})

    @property
    def action_space(self):
        return self._env.action_space

    def close(self):
        return self._env.close()

    def reset(self):
        with self.LOCK:
            self._env.reset()
        noops = self._random.randint(1, self._noops + 1)
        for _ in range(noops):
            done = self._env.step(0)[2]
            if done:
                with self.LOCK:
                    self._env.reset()
        self._lives = self._env.ale.lives()
        if self._grayscale:
            self._env.ale.getScreenGrayscale(self._buffers[0])
        else:
            self._env.ale.getScreenRGB2(self._buffers[0])
        self._buffers[1].fill(0)
        return self._get_obs()

    def step(self, action):
        total_reward = 0.0
        for step in range(self._action_repeat):
            _, reward, done, info = self._env.step(action)
            total_reward += reward
            if self._life_done:
                lives = self._env.ale.lives()
                done = done or lives < self._lives
                self._lives = lives
            if done:
                break
            elif step >= self._action_repeat - 2:
                index = step - (self._action_repeat - 2)
                if self._grayscale:
                    self._env.ale.getScreenGrayscale(self._buffers[index])
                else:
                    self._env.ale.getScreenRGB2(self._buffers[index])
        obs = self._get_obs()
        return obs, total_reward, done, info

    def render(self, mode):
        return self._env.render(mode)

    def _get_obs(self):
        if self._action_repeat > 1:
            np.maximum(self._buffers[0], self._buffers[1], out=self._buffers[0])
        image = np.array(Image.fromarray(self._buffers[0]).resize(
            self._size, Image.BILINEAR))
        image = np.clip(image, 0, 255).astype(np.uint8)
        image = image[:, :, None] if self._grayscale else image
        return {'image': image}


class Collect:

    def __init__(self, env, callbacks=None, precision=32):
        self._env = env
        self._callbacks = callbacks or ()  # 用or 实现 判断
        self._precision = precision
        self._episode = None

    def __getattr__(self, name):  # 继承
        return getattr(self._env, name)

    def step(self, action):
        obs, reward, done, info = self._env.step(action)  # 直接 env step
        obs = {k: self._convert(v) for k, v in obs.items()}  # obs {k,v}->{k,convert(v)}
        transition = obs.copy()  # 字典
        transition['action'] = action
        transition['reward'] = reward
        transition['discount'] = info.get('discount', np.array(1 - float(done)))
        self._episode.append(transition)  # 添加每帧到episodes中
        if done:
            episode = {k: [t[k] for t in self._episode] for k in self._episode[0]}
            episode = {k: self._convert(v) for k, v in episode.items()}
            info['episode'] = episode
            for callback in self._callbacks:
                callback(episode)  # 存储数据
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        transition = obs.copy()
        transition['action'] = np.zeros(self._env.action_space.shape)
        transition['reward'] = 0.0
        transition['discount'] = 1.0
        self._episode = [transition]
        return obs

    def _convert(self, value):
        value = np.array(value)
        if np.issubdtype(value.dtype, np.floating):
            dtype = {16: np.float16, 32: np.float32, 64: np.float64}[self._precision]
        elif np.issubdtype(value.dtype, np.signedinteger):
            dtype = {16: np.int16, 32: np.int32, 64: np.int64}[self._precision]
        elif np.issubdtype(value.dtype, np.uint8):
            dtype = np.uint8
        else:
            raise NotImplementedError(value.dtype)
        return value.astype(dtype)


class TimeLimit:

    def __init__(self, env, duration):
        self._env = env
        self._duration = duration  # 这里是500步长的限制
        self._step = None  # 新加了步长计数

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        assert self._step is not None, 'Must reset environment.'
        obs, reward, done, info = self._env.step(action)
        self._step += 1
        if self._step >= self._duration:
            done = True  # 步长到了，强制结束
            if 'discount' not in info:  # 为何顺带处理 discount的问题？
                info['discount'] = np.array(1.0).astype(np.float32)
            self._step = None
        return obs, reward, done, info

    def reset(self):
        self._step = 0
        return self._env.reset()


class NaturalMujoco:
    """
    添加背景
    """

    def __init__(self, env, dataset):
        self.dataset = dataset
        self._pointer = (np.random.randint(self.dataset.shape[0]), 0)
        self._env = env

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        obs = self._noisify_obs(obs, done)
        return obs, reward, done, info

    def _noisify_obs(self, obs, done):
        obs = obs.copy()
        img = obs['image']
        video_id, img_id = self._pointer

        # ugly hack to extract only yellow pixels
        fgmask = (img[:, :, 0] > 100)[:, :, None].repeat(3, axis=2)
        if done:
            video_id = np.random.randint(self.dataset.shape[0])
            img_id = 0
        else:
            img_id = (img_id + 1) % self.dataset.shape[1]
        background = self.dataset[video_id, img_id]
        img = img * fgmask + background * (~fgmask)
        self._pointer = (video_id, img_id)
        obs['image'] = img
        return obs

    def reset(self):
        obs = self._env.reset()
        obs = self._noisify_obs(obs, False)
        return obs


class AudioMujoco:
    """
    add background/contact sound for MuJoCo task
    """

    def __init__(self, env, sound):
        self._sound = sound
        self._env = env
        self._ncon = 0
        self._t = 0

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        obs = self._sound_obs(obs, done)
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        obs = self._sound_obs(obs, False)
        return obs

    def _sound_obs(self, obs, done):
        obs = obs.copy()
        #ncon = obs['n_contact']
        #audio = self._sound["back_ground"][self._t:self._t + 4410]
        #self._t = (self._t + 4410) % (self._sound["back_ground"].shape[0] - 4410)

        #if ncon > self._ncon:
        #    audio = audio // 2 + self._sound["audio_data"][:4410] // 2  # add a contact sound
        #if done:
        #    self._ncon = 0
        #else:
        #    self._ncon = ncon
        #obs['audio'] = audio.reshape(-1)
        return obs


class MissingMultimodal:
    """
    丢失数据
    """

    def __init__(self, env, config):
        self._env = env
        self._c = config
        self._t = 0
        self._drop_end = dict()

    def __getattr__(self, name):
        return getattr(self._env, name)

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        obs = self._missing_obs(obs)
        self._t += 1
        return obs, reward, done, info

    def _missing_obs(self, obs):
        obs_c = obs.copy()
        for key, value in obs.items():  # 遍历所有observation
            if key in self._c.miss_ratio:  # 判断是否有丢失的必要
                value_f = value
                flag = np.array([1]).astype(np.float16)
                if self._t < self._drop_end.get(key, 0):  # 继续丢失
                    value_f = 0 * value
                    flag = 0 * flag
                elif np.random.rand() <= self._c.miss_ratio.get(key, -1.0):  # 得到丢失比率
                    value_f = 0 * value
                    flag = 0 * flag
                    self._drop_end[key] = self._t + np.random.randint(1, self._c.max_miss_len)
                obs_c[key] = value_f
                obs_c[key + '_flag'] = flag
        return obs_c

    def reset(self):
        self._t = 0
        self._drop_end = dict()
        obs = self._env.reset()
        obs = self._missing_obs(obs)
        return obs

    # class MissingMultimodal:
    #     """
    #     丢失数据
    #     """
    #
    #     def __init__(self, env, config):
    #         self._env = env
    #         self._c = config
    #
    #     def __getattr__(self, name):
    #         return getattr(self._env, name)
    #
    #     def step(self, action):
    #         obs, reward, done, info = self._env.step(action)
    #         obs = self._missing_obs(obs)
    #         return obs, reward, done, info
    #
    #     def _missing_obs(self, obs):
    #         obs = obs.copy()
    #         img = obs['image']
    #         depth = obs["depth"]
    #         touch = obs["touch"]
    #         audio = obs["audio"]
    #
    #         dep_flag = np.array([1]).astype(np.float16)
    #         img_flag = np.array([1]).astype(np.float16)
    #         touch_flag = np.array([1]).astype(np.float16)
    #         audio_flag = np.array([1]).astype(np.float16)
    #         if np.random.rand() <= self._c.miss_ratio_r:
    #             img = 0 * img
    #             img_flag = 0 * img_flag
    #         if np.random.rand() <= self._c.miss_ratio_d:
    #             depth = 0 * depth
    #             dep_flag = 0 * dep_flag
    #         if np.random.rand() <= self._c.miss_ratio_t:
    #             touch = 0 * touch
    #             touch_flag = 0 * touch_flag
    #         if np.random.rand() <= self._c.miss_ratio_a:
    #             audio = 0 * audio
    #             audio_flag = 0 * audio_flag
    #
    #         obs['image'] = img
    #         obs['depth'] = depth
    #         obs['touch'] = touch
    #         obs['audio'] = audio
    #         obs['img_flag'] = img_flag
    #         obs['dep_flag'] = dep_flag
    #         obs['touch_flag'] = touch_flag
    #         obs['audio_flag'] = audio_flag
    #         return obs

    def reset(self):
        obs = self._env.reset()
        obs = self._missing_obs(obs)
        return obs


class ActionRepeat:
    """
    相同的action执行很多次，只是记录最后的obs，reward相加
    """

    def __init__(self, env, amount):
        self._env = env
        self._amount = amount

    def __getattr__(self, name):  # 测试下 是否完全继承
        return getattr(self._env, name)

    def step(self, action):
        done = False
        total_reward = 0
        current_step = 0
        while current_step < self._amount and not done:
            obs, reward, done, info = self._env.step(action)
            total_reward += reward
            current_step += 1
        return obs, total_reward, done, info


class NormalizeActions:
    """
    Yong Lee, 为什么要对Action 进行归一化？
    原来我们的policy给出的action 位于【-1,1】之间
    而，真正的action space 位于【low, high】之间
    """

    def __init__(self, env):
        self._env = env
        self._mask = np.logical_and(
            np.isfinite(env.action_space.low),
            np.isfinite(env.action_space.high))
        self._low = np.where(self._mask, env.action_space.low, -1)
        self._high = np.where(self._mask, env.action_space.high, 1)

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def action_space(self):
        low = np.where(self._mask, -np.ones_like(self._low), self._low)
        high = np.where(self._mask, np.ones_like(self._low), self._high)
        return gym.spaces.Box(low, high, dtype=np.float32)

    def step(self, action):
        original = (action + 1) / 2 * (self._high - self._low) + self._low
        original = np.where(self._mask, original, action)
        return self._env.step(original)


class ObsDict:

    def __init__(self, env, key='obs'):
        self._env = env
        self._key = key

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def observation_space(self):
        spaces = {self._key: self._env.observation_space}
        return gym.spaces.Dict(spaces)

    @property
    def action_space(self):
        return self._env.action_space

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        obs = {self._key: np.array(obs)}
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        obs = {self._key: np.array(obs)}
        return obs


class OneHotAction:

    def __init__(self, env):
        assert isinstance(env.action_space, gym.spaces.Discrete)
        self._env = env

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def action_space(self):
        shape = (self._env.action_space.n,)
        space = gym.spaces.Box(low=0, high=1, shape=shape, dtype=np.float32)
        space.sample = self._sample_action
        return space

    def step(self, action):
        index = np.argmax(action).astype(int)
        reference = np.zeros_like(action)
        reference[index] = 1
        if not np.allclose(reference, action):
            raise ValueError(f'Invalid one-hot action:\n{action}')
        return self._env.step(index)

    def reset(self):
        return self._env.reset()

    def _sample_action(self):
        actions = self._env.action_space.n
        index = self._random.randint(0, actions)
        reference = np.zeros(actions, dtype=np.float32)
        reference[index] = 1.0
        return reference


class RewardObs:

    def __init__(self, env):
        self._env = env

    def __getattr__(self, name):
        return getattr(self._env, name)

    @property
    def observation_space(self):
        spaces = self._env.observation_space.spaces
        assert 'reward' not in spaces
        spaces['reward'] = gym.spaces.Box(-np.inf, np.inf, dtype=np.float32)
        return gym.spaces.Dict(spaces)

    def step(self, action):
        obs, reward, done, info = self._env.step(action)
        obs['reward'] = reward
        return obs, reward, done, info

    def reset(self):
        obs = self._env.reset()
        obs['reward'] = 0.0
        return obs


class Async:  # 异步通讯？Yong Lee表示不懂

    _ACCESS = 1
    _CALL = 2
    _RESULT = 3
    _EXCEPTION = 4
    _CLOSE = 5

    def __init__(self, ctor, strategy='process'):
        self._strategy = strategy
        if strategy == 'none':
            self._env = ctor()
        elif strategy == 'thread':
            import multiprocessing.dummy as mp
        elif strategy == 'process':
            import multiprocessing as mp
        else:
            raise NotImplementedError(strategy)
        if strategy != 'none':
            self._conn, conn = mp.Pipe()
            self._process = mp.Process(target=self._worker, args=(ctor, conn))
            atexit.register(self.close)
            self._process.start()
        self._obs_space = None
        self._action_space = None

    @property
    def observation_space(self):
        if not self._obs_space:
            self._obs_space = self.__getattr__('observation_space')
        return self._obs_space

    @property
    def action_space(self):
        if not self._action_space:
            self._action_space = self.__getattr__('action_space')
        return self._action_space

    def __getattr__(self, name):
        if self._strategy == 'none':
            return getattr(self._env, name)
        self._conn.send((self._ACCESS, name))
        return self._receive()

    def call(self, name, *args, **kwargs):
        blocking = kwargs.pop('blocking', True)
        if self._strategy == 'none':
            return functools.partial(getattr(self._env, name), *args, **kwargs)
        payload = name, args, kwargs
        self._conn.send((self._CALL, payload))
        promise = self._receive
        return promise() if blocking else promise

    def close(self):
        if self._strategy == 'none':
            try:
                self._env.close()
            except AttributeError:
                pass
            return
        try:
            self._conn.send((self._CLOSE, None))
            self._conn.close()
        except IOError:
            # The connection was already closed.
            pass
        self._process.join()

    def step(self, action, blocking=True):
        return self.call('step', action, blocking=blocking)

    def reset(self, blocking=True):
        return self.call('reset', blocking=blocking)

    def _receive(self):
        try:
            message, payload = self._conn.recv()
        except ConnectionResetError:
            raise RuntimeError('Environment worker crashed.')
        # Re-raise exceptions in the main process.
        if message == self._EXCEPTION:
            stacktrace = payload
            raise Exception(stacktrace)
        if message == self._RESULT:
            return payload
        raise KeyError(f'Received message of unexpected type {message}')

    def _worker(self, ctor, conn):
        try:
            env = ctor()
            while True:
                try:
                    # Only block for short times to have keyboard exceptions be raised.
                    if not conn.poll(0.1):
                        continue
                    message, payload = conn.recv()
                except (EOFError, KeyboardInterrupt):
                    break
                if message == self._ACCESS:
                    name = payload
                    result = getattr(env, name)
                    conn.send((self._RESULT, result))
                    continue
                if message == self._CALL:
                    name, args, kwargs = payload
                    result = getattr(env, name)(*args, **kwargs)
                    conn.send((self._RESULT, result))
                    continue
                if message == self._CLOSE:
                    assert payload is None
                    break
                raise KeyError(f'Received message of unknown type {message}')
        except Exception:
            stacktrace = ''.join(traceback.format_exception(*sys.exc_info()))
            print(f'Error in environment process: {stacktrace}')
            conn.send((self._EXCEPTION, stacktrace))
        conn.close()
