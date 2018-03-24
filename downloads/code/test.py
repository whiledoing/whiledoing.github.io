#-*- coding: utf-8 -*-

from common.rpcdecorator import SERVER_ONLY, rpc_method
from common.RpcMethodArgs import Int, EntityID, MailBox, Dict, List, Str
from common.IdManager import IdManager
from mobilelog.LogManager import LogManager
from servercommon.ServerSpace import ServerSpace

from hexm.server.utils import misc
from hexm.server import G
from hexm.common import errcode
from hexm.server.consts import space_consts
from hexm.server import hex_api
from hexm.common.datetime_manager import DateTimeManager
from hexm.server.stub.stub_base import StubBase, create_local_global_stub
from hexm.server.stub.space.world_boss.world_boss_stub import WorldBossStub
from hexm.common import portable
from hexm.common import space_common

import sys
import random

def create_local_space_stub():
	return create_local_global_stub(SpaceStub, 'SpaceStub', need_in_game_sid = 0)

class SpaceManagerInStub(object):
	'''
		Here store the SpaceEntity Mailbox, reserved
		SpaceStub is global entity, so can get entity from gamemanager
	'''

	_reload_all = True

	NEED_BACKUP_KEY_LIST = ['spaceno', 'spaceid', 'seq']

	def __init__(self, stub, spaceno):
		self.logger = LogManager.get_logger(self.__class__.__name__)
		self.spaceno = spaceno
		self.stub = stub
		self.spaces = dict()
		self.queue = dict()
		self.space_managers = dict()
		self.creating = {}
		# for query team access to space
		self.team_space_mapping = {}
		# cache team access to space
		self.space_team_mapping = {}

		# 分线信息统计, 和self.spaces结构保持对应关系
		self.sep_line_info = {'spaceno' : spaceno}

		# 是否需要分线统计, 比如副本之类的space可能就不需要分线逻辑
		# @note 设置为构造时候设定是因为, 理论上说, 该变量不可以后续改变
		# @note @TODO 以后需要留意, 如果启动的时候将stub和普通的game进程区别开, 需要启动的时候也将G.spacem进行设定
		self.need_sep = space_common.is_main_world(self.spaceno)

	def get_all_spaces_id_list(self):
		return self.spaces.keys()

	def get_all_space_num(self):
		return len(self.spaces)

	def is_space_need_sep(self):
		return self.need_sep

	def get_space_sep_line_info(self):
		return self.sep_line_info

	def _add_to_queue(self, applier, spaceid, ctrl_info):
		if spaceid not in self.queue:
			self.queue[spaceid] = []
		self.queue[spaceid].append([applier, ctrl_info])

	def create_space(self, spaceid, teamid=None, controller=None):
		map_no = G.datam.get("scene_info").get(self.spaceno, {}).get("map_no", None)
		resource_name = G.datam.get("map").get(map_no, {}).get("resources", None)
		if not resource_name:
			self.logger.critical('[MEET_INVALID_MAPFILE_CONFIG] spaceno %s' % self.spaceno)
			return False

		if not self.is_valid_spaceid(spaceid):
			self.logger.error('[CRATE_SPACE_FAIL_MEET_INVALID_SPACEID] invalid spaceid %s' % spaceid)
			return False

		# 正在创建就继续等等, 也是成功
		if self.creating.get(spaceid, False):
			return True

		# 表示创建中, 否则重复创建
		self.creating[spaceid] = True

		# @note 这里指定一个特定的spaceid, 便于log中统计创建space的信息.
		# 但是self.creating中却需要使用None的spaceid, 因为这个None表示当前玩家不知道进入哪一个分线, 然后
		# 也没有合适的分线进入情况下的创建, 使用None统一标记创建状态, 这样子当同时多个None实例创建的时候,
		# 就可以让其等待, 而不是创建多个不同的id的space
		if spaceid is None: spaceid = IdManager.genid()
		if teamid:
			self.team_space_mapping[teamid] = spaceid
			self.space_team_mapping[spaceid] = teamid

		if controller is None: controller = {}
		ServerSpace.create_space(
			mapfile = misc.get_mapfile(resource_name),
			controller = controller,
			spaceno = self.spaceno,
			spaceid = spaceid,
			anywhere = True
		)

		self.logger.info('[CREATE_SPACE] space no %s - space id %s' % (self.spaceno, spaceid))
		return True

	def create_space_and_call_init_func(self, spaceid, init_fuc, *args, **kwargs):
		return self.create_space(spaceid, teamid=None, controller={
			'init_func' : {
				'name' : init_fuc,
				'args' : args,
				'kwargs' : kwargs
			}
		})

	def _choice_one_space_according_to_spaceid(self, spaceid, ctrl_info):
		''' 根据spaceid选择space实体, 如果spaceid为None, 那么随机选择, 如果是需要分线的话, 根据分线逻辑进行选择 '''
		if spaceid is None:
			if self.need_sep:
				return self._choice_one_space_according_to_sep_line_rule(ctrl_info)
			else:
				# @note 理论上, 这里不应该跑到, 因为当前没有相关逻辑是玩家进入不是分线场景, 然后随机从生成的地方选择
				# 如果是副本, 那么是在创建副本的地方设定好spaceid, 然后进行调用.
				return self.spaces[random.choice(self.spaces.keys())] if self.spaces else None
		else:
			return self.spaces.get(spaceid, None)

	def _choice_one_space_according_to_ctrl_info(self, ctrl_info):
		if not ctrl_info: return None

		# refs #1997 如果存在队长所在分线，使用队长所在分线。
		leader_spaceid = ctrl_info.get('leader_spaceid', None)
		if leader_spaceid and leader_spaceid in self.spaces: return self.spaces[leader_spaceid]

		# refs #1997 否则使用人数最多的分线（无视人数规则）
		spaceid_num_counter = ctrl_info.get('spaceid_num_counter', None)
		if not spaceid_num_counter: return None

		# 去掉无效的数据
		spaceid_num_counter = {num : spaceid for spaceid, num in spaceid_num_counter.iteritems() if spaceid in self.spaces}
		if not spaceid_num_counter: return None

		# 得到最大人数下的space
		return self.spaces[sorted(spaceid_num_counter.iteritems(), reverse = True)[0][1]]

	def _choice_one_space_according_to_sep_line_rule(self, ctrl_info):
		''' 根据分线规则指定一个space '''

		# 没有任何space, 返回找不到
		if not self.spaces: return None

		# refs #1997 优先根据控制参数结果选择分线
		ctrl_res = self._choice_one_space_according_to_ctrl_info(ctrl_info)
		if ctrl_res: return ctrl_res

		# 目前规则是:
		# 1) 找到不超过上线的所有space
		# 2) 编号最小
		# 3) 如果都不符合, 那么重新创建
		seq_sid_list = []
		for sid in self.spaces.iterkeys():
			v = self.sep_line_info.get('mem', {}).get(sid, None)
			if not v: continue

			seq = v.get('seq', None)
			if seq is None: continue

			num = v.get('num', sys.maxint)
			if num >= space_consts.ONE_SPACE_MAX_HOLD_NUM: continue

			seq_sid_list.append((seq, sid))

		# 找到编号最小且存在的第一个有效space
		return self.spaces.get(sorted(seq_sid_list)[0][1], None) if seq_sid_list else None

	def _get_min_valid_seq_num(self):
		sep_mem = self._setdefault_sep_line_mem_info()
		seq_list = [v['seq'] for v in sep_mem.itervalues() if 'seq' in v]
		if not seq_list: return space_consts.MIN_SPACE_LINE_SEQ_NUM

		# 顺序的seq, 直接得到最大值+1
		max_v, cur_n = max(seq_list), len(seq_list)
		if space_consts.MIN_SPACE_LINE_SEQ_NUM + cur_n - 1 == max_v: return max_v + 1

		# 找到第一个不在seq中的最小值, 这样子查找是防止以后出现消除分线编号的情况
		seq_set = set(seq_list)
		for i in range(space_consts.MIN_SPACE_LINE_SEQ_NUM, max_v):
			if i not in seq_set:
				return i
		return max_v + 1

	def _setdefault_sep_line_mem_info(self):
		return self.sep_line_info.setdefault('mem', {})

	def _setdefault_sep_line_one_mem_info(self, spaceid):
		''' 得到特定id下的分线信息 @note spaceid, 从self.spaces中获取, 如果不存在分线信息, 会进行touch操作 '''
		sep_mem = self._setdefault_sep_line_mem_info()
		if spaceid in sep_mem:
			cur_space_sep_info = sep_mem[spaceid]
		else:
			cur_space_sep_info = {
				'num' : 0,
				'seq' : self._get_min_valid_seq_num(),
				'status' : self._get_space_running_status(0),
				'spaceno' : self.spaceno,
				'spaceid' : spaceid
			}
			sep_mem[spaceid] = cur_space_sep_info

		return cur_space_sep_info

	def _get_space_running_status(self, num):
		'''根据当前人数, 重新确认运行状态'''
		if num < space_consts.ONE_SPACE_MAX_HOLD_NUM / 2:
			return 'idle'
		elif num < space_consts.ONE_SPACE_MAX_HOLD_NUM:
			return 'busy'
		else:
			return 'hot'

	def _get_space_seq_num_from_spaceid(self, spaceid):
		return self.sep_line_info.get('mem', {}).get(spaceid, {}).get('seq', None) if spaceid else None

	def _call_applier_teleport_into_space(self, applier, space, ctrl_info, record_num_in_sep_line = True):
		if not space: return

		# 根据规则得到一个有效的位置和朝向
		position = G.spacem.get_spaceno_position_from_ctrl_info(self.spaceno, ctrl_info)
		direction = G.spacem.get_spaceno_direction_from_ctrl_info(self.spaceno, ctrl_info)

		# 玩家转移到特定space所在的进程
		if portable.engine_version() < 1094:
			self.stub.call(applier, 'teleport', space, position, direction)
		else:
			self.stub.call(applier, 'teleport', space, self.spaceno, position, direction)

		if self.need_sep:
			cur_space_sep_info = self._setdefault_sep_line_one_mem_info(space.id)

			# @note 这里也累加人数, 如果不累加, 担心一段时间大数量的人涌入, 然后space也没有及时汇报人数, 导致人数全部都进入
			# 到了当前的space中, 所以这里先认为玩家可以成功进入space, 然后等到实际汇报数据来了之后再强制写回, 现在这样子计算其实
			# 提高了要求, 肯定也不是坏事.
			if record_num_in_sep_line:
				cur_space_sep_info['num'] = cur_space_sep_info.get('num', 0) + 1
				cur_space_sep_info['status'] = self._get_space_running_status(cur_space_sep_info['num'])

			self.logger.debug('[APPLIER_TELEPORT_INTO_SPACE] applier id %s - space no %s - space id %s - space sep line %s' % (applier.id, self.spaceno, space.id, cur_space_sep_info))
		else:
			self.logger.debug('[APPLIER_TELEPORT_INTO_SPACE] applier id %s - space no %s - space id %s' % (applier.id, self.spaceno, space.id))

	def is_valid_spaceid(self, spaceid):
		return True if spaceid is None else IdManager.is_valid_id(spaceid)

	def apply_enter_space(self, applier, spaceid, ctrl_info = None):
		# 如果不设定rpc参数, 兼容的概念就是不指定id
		if spaceid == "": spaceid = None

		# 后续所有的ctrl_info都默认不做None的检测了
		if ctrl_info is None: ctrl_info = {}

		if not self.is_valid_spaceid(spaceid):
			self.logger.error('[ENTER_SPACE_FAIL_MEET_INVALID_SPACEID] invalid spaceid %s' % spaceid)
			return self.stub.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_INVALID_SPACEID, para = {"spaceno": self.spaceno}, ctrl_info = ctrl_info)

		space = self._choice_one_space_according_to_spaceid(spaceid, ctrl_info)

		# 如果还有备用的方案, 那么尝试进入备用场景, 否则根据id创建新的space
		if space is None:
			if not self.need_sep and not self.creating.get(spaceid, False):
				self.logger.error("enter space id %s but cannot find proper space" % spaceid)
				return self.stub.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_INVALID_SPACEID, para = {"spaceno": self.spaceno}, ctrl_info = ctrl_info)
			"""
			backup_space = ctrl_info.pop('backup_space', {}) if ctrl_info else {}
			if backup_space:
				self.logger.info('[APPLY_ENTER_SPACE_CHANGE_TO_BACKUP_SPACE] cur spaceno %s, cur spaceid %s, backup space info %s' % (self.spaceno, spaceid, backup_space))
				return self._apply_enter_space_in_backup_space(applier, backup_space)
			"""

			# 标记进入等待状态 @TODO 世界场景找不到时尝试创建暂时放在这里
			self._add_to_queue(applier, spaceid, ctrl_info)

			# 创建, 并检测创建结果
			if not self.create_space(spaceid):
				return self.stub.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_SPACE_CREATED_FAIL, para = {"spaceno": self.spaceno}, ctrl_info = ctrl_info)
		else:
			self._call_applier_teleport_into_space(applier, space, ctrl_info)

	def apply_create_and_enter_space(self, applier, spaceid, teamid, ctrl_info = None):
		if spaceid == "": spaceid = None

		# 后续所有的ctrl_info都默认不做None的检测了
		if ctrl_info is None: ctrl_info = {}

		if not self.is_valid_spaceid(spaceid):
			self.logger.error('[ENTER_SPACE_FAIL_MEET_INVALID_SPACEID] invalid spaceid %s' % spaceid)
			return self.stub.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_INVALID_SPACEID, para = {"spaceno": self.spaceno}, ctrl_info = ctrl_info)

		space = self._choice_one_space_according_to_spaceid(spaceid, ctrl_info)

		if space is None:
			# 标记进入等待状态
			self._add_to_queue(applier, spaceid, ctrl_info)

			# 创建, 并检测创建结果
			if not self.create_space(spaceid, teamid):
				return self.stub.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_SPACE_CREATED_FAIL, para = {"spaceno": self.spaceno}, ctrl_info = ctrl_info)
		else:
			# refs #6418 逻辑基本和`apply_enter_space`相同, 这里的逻辑是, 如果没有创建, 如果有了那么进入。 而前者的逻辑是, 如果没有, 如果是分线场景可以创建, 不是分线场景的话, 必须要有场景才可以, 不可以自动创建。
			self._call_applier_teleport_into_space(applier, space, ctrl_info)

	def _apply_enter_space_in_backup_space(self, applier, backup_space):
		# 将back的信息取得, 然后作为ctrl_info重新进入新的backup场景
		spaceno, spaceid = backup_space.pop('spaceno', None), backup_space.pop('spaceid', None)
		return self.stub.apply_enter_space(applier, spaceno, spaceid, backup_space)

	def on_space_created(self, space):
		self.logger.info('[CREATE_SPACE_SUCCESS] space no %s - space id %s' % (self.spaceno, space.id))

		self.spaces[space.id] = space

		# touch 出来分线的信息, 这样子默认创建的space, 也可以获得通过分线算法计算分配逻辑
		if self.need_sep:
			self._setdefault_sep_line_one_mem_info(space.id)

			# @note 放在这里是因为要将当前touch出来的分线信息同步一下, 这样子刚登陆进入的玩家可以看到对应分线信息
			# @note 还有一点小偏差在于touch出来的人数是不太准确的, 不过统计的其实也不准确, 所以现在按照这个逻辑进行没啥问题.
			# @note 立刻推送消息(有0.5流量限制延迟). 如果消息应该是肯定给到新玩家的, 如果新玩家比消息到的早, 没有问题, 到的迟, 最新消息到了
			# 玩家上线之后也会立刻pull一下最新分线, 所以也是对的
			self.on_meet_new_sep_line_spaceid(space.id)

		if space.id in self.queue:
			for applier, ctrl_info in self.queue.get(space.id, []):
				self._call_applier_teleport_into_space(applier, space, ctrl_info)
			self.queue.pop(space.id)

		if None in self.queue:
			for applier, ctrl_info in self.queue.get(None, []):
				self._call_applier_teleport_into_space(applier, space, ctrl_info)
			self.queue.pop(None)

		if space.id in self.creating:
			del self.creating[space.id]

		if None in self.creating:
			del self.creating[None]

	def on_space_destroy(self, spaceid):
		# don't need return, pop unused mappings
		# if spaceid not in self.spaces:
		# 	return

		# navigate.remove_navmap(spaceid)

		# 去掉所有关联状态
		self.spaces.pop(spaceid, None)
		self.creating.pop(spaceid, None)
		self._setdefault_sep_line_mem_info().pop(spaceid, None)

		teamid = self.space_team_mapping.pop(spaceid, None)
		self.team_space_mapping.pop(teamid, None)

		# 去掉所有等待进入的玩家信息, 虽然不太可能出现等待创建过程中, 然后通知说销毁了
		for applier, ctrl_info in self.queue.get(spaceid, []):
			self.stub.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_SPACE_CREATED_FAIL, para = {"spaceno": self.spaceno}, ctrl_info = ctrl_info)

	def _on_syn_one_space_sep_line_info(self, spaceid, game_sep_line_info):
		# 更新game级别回报来的分线信息
		if not game_sep_line_info: return
		cur_space_sep_info = self._setdefault_sep_line_one_mem_info(spaceid)

		# 保留字段, 别的都从推送数据中获取, 然后使用备份数据覆盖最新数据.
		back_info = {key:cur_space_sep_info[key] for key in self.NEED_BACKUP_KEY_LIST if key in cur_space_sep_info}

		# @note 使用clear, 因为需要改变原始的数据
		cur_space_sep_info.clear()
		cur_space_sep_info.update(game_sep_line_info)
		cur_space_sep_info.update(back_info)

		# 别的字段全部按照game级别数据来处理
		cur_space_sep_info.update(game_sep_line_info)

		# 重新计算人数对应状态
		cur_space_sep_info['status'] = self._get_space_running_status(cur_space_sep_info.get('num', 0))

	def syn_space_sep_line_info(self, syn_info, time_to_client_dict):
		if not self.is_space_need_sep(): return

		# 优先设置普通数据
		for spaceid, v in syn_info.iteritems():
			if spaceid not in self.spaces: continue
			if not v: continue

			self._on_syn_one_space_sep_line_info(spaceid, v)

			# @note 加急的信息, 目前的逻辑就是一个tag, 然后标记当前的spaceno下的客户端这个消息是加急的
			# @note 目前的流程是, game级别做时间的缓冲, 到了stub级别就是直接的推送, 同时为了考虑推送压力, 加入最小时间间隔进行限流.
			if spaceid in time_to_client_dict:
				self.on_meet_time_to_client(spaceid)

	def on_meet_time_to_client(self, spaceid, new_delay = 0):
		self.stub.on_meet_time_to_client(self.spaceno, spaceid, new_delay = new_delay)

	def on_meet_new_sep_line_spaceid(self, spaceid, new_delay = 0):
		self.stub.on_meet_new_sep_line_spaceid(self.spaceno, spaceid, self._get_space_seq_num_from_spaceid(spaceid), new_delay = new_delay)

	def call_all_space_method(self, fun_name, args, kwargs):
		for spaceid in self.spaces.keys():
			self.call_one_space_method(spaceid, fun_name, args, kwargs)

	def call_one_space_method(self, spaceid, fun_name, args, kwargs):
		if spaceid not in self.spaces: return
		if args is None: args = []
		if kwargs is None: kwargs = {}
		self.stub.call(self.spaces[spaceid], 'rpc_call_method_from_stub', fun_name, args, kwargs)

	def get_spaceid_by_teamid(self, teamid):
		return self.team_space_mapping.get(teamid, "")

	def get_space_mailbox_by_spaceid(self, spaceid):
		return self.spaces.get(spaceid, "")

	def update_sep_line_data_immediately(self, spaceid, data, world_boss_info_change = False):
		"""
		立刻推送sep_line的数据, 这里就不进行时间的控制, 目前也主要用于boss信息的变化

		@deprecated 不要在stub级别直接修改数据, 这样子破坏了game级别单项缓存数据的一致性, 除非是只修改stub级别才可以修改的数据除外.
		但是目前没有相对应的业务逻辑, 所以该接口暂时废弃, 不允许使用.
		"""
		if not spaceid or not data: return

		# 这里的结构设计就必须是立刻的消息, 到了这里就是需要立刻推送的
		self.syn_space_sep_line_info({spaceid: data}, time_to_client_dict={spaceid: True})

		# 如果不是立刻推送, 需要hook syn_space_sep_line_info的逻辑, 大概需要实现为:
		# self._on_syn_one_space_sep_line_info(spaceid, data)
		# self.on_meet_time_to_client(delay)

		if world_boss_info_change:
			self.stub._on_world_boss_info_change()

	def try_destroy_space_when_team_is_dismissed(self, teamid):
		spaceid = self.get_spaceid_by_teamid(teamid)
		self.call_one_space_method(spaceid, "kick_all_avatar_and_destroy", None, None)

class SpaceStub(StubBase):
	def __init__(self, eid):
		super(SpaceStub, self).__init__(eid)

		self.space_managers = {}
		self._need_to_push_ctrl_info = {}
		self._world_boss_stub = WorldBossStub(self)

	def init_from_dict(self, bdict):
		super(SpaceStub, self).init_from_dict(bdict)

		from hexm.server.utils.entity_timer_manager_wrapper import EntityTickerKeyTimerManager
		self._ticker_mgr = EntityTickerKeyTimerManager(self)
		self._ticker_mgr.run_ticker()

		# @TEMP 预先生成默认数量的space
		# @note 如果创建太快, 引擎那边会导致on_load失败, 不知道为啥, 先简单, 每隔一段时间创建一个分线
		from hexm import debug_profile
		scene_no, count = debug_profile.get_server_default_scene_config()
		self._ticker_mgr.add_ticker_key_repeat_timer('create_space', 0.5, lambda : self.apply_create_space(scene_no, IdManager.genid()), run_num=count)

		# 自动每隔一段时间推送当前所有的分线列表信息到game进程
		self._ticker_mgr.add_ticker_key_repeat_timer(
			space_consts.STUB_TICKER_TIMER_KEY_PUSH_SEP_LINE,
			space_consts.STUB_TIME_INTERVAL_PUSH_SEP_LINE,
			self._on_tick_send_sep_line
		)

		# 更新世界BOSS玩法状态和同步数据
		self._ticker_mgr.add_ticker_key_repeat_timer(
			space_consts.STUB_TICKER_TIMER_KEY_WORLD_BOSS,
			space_consts.STUB_TIME_INTERVAL_WORLD_BOSS,
			self._on_tick_world_boss
		)

	def reload_script(self):
		self._ticker_mgr.change_ticker_key_repeater_timer_delay(
			space_consts.STUB_TICKER_TIMER_KEY_PUSH_SEP_LINE,
			space_consts.STUB_TIME_INTERVAL_PUSH_SEP_LINE
		)

	def change_push_sep_line_next_delay(self, new_delay):
		# 有最小时间间隔保证流量压力
		new_delay = max(abs(new_delay), space_consts.STUB_MIN_TIME_INTERVAL_PUSH_SEP_LINE)
		self._ticker_mgr.change_ticker_key_next_run_delay(
			space_consts.STUB_TICKER_TIMER_KEY_PUSH_SEP_LINE,
			new_delay,
			force=False,
			log_timer=False
		)

	def on_meet_time_to_client(self, spaceno, spaceid = None, new_delay = 0, set_new_delay = True):
		'''遇到加急的消息, 那么标记, 并立刻推送给game级别, 然后推送到client'''
		mgr = self.space_managers.get(spaceno, None)
		if mgr is None: return

		spaceno_info = self._need_to_push_ctrl_info.setdefault('space_ctrl_info', {}).setdefault(spaceno, {})
		spaceno_info['syn_now'] = True

		# @note 现在逻辑是, 如果有推送, 那么整个spaceno下的所有分线都立刻同步一次
		mem_info = spaceno_info.setdefault('mem', {})
		for one_id in mgr.get_all_spaces_id_list():
			mem_info.setdefault(one_id, {})['syn_now'] = True

		# 标记实际变化的id, @note 如果没有设置, 表示只是通知当前spaceno下数据变化, 不具体针对某一个id下的数据变化
		if spaceid is not None:
			mem_info.setdefault(spaceid, {})['change_now'] = True

		if set_new_delay:
			self.change_push_sep_line_next_delay(new_delay)

	def on_meet_new_sep_line_spaceid(self, spaceno, spaceid, seq_num, new_delay = 0):
		'''遇到建立了新的分线, 标记, 同时表示加急的消息, 需要推送到client'''
		spaceno_info = self._need_to_push_ctrl_info.setdefault('space_ctrl_info', {}).setdefault(spaceno, {})
		spaceno_info.setdefault('mem', {}).setdefault(spaceid, {})['new'] = True
		self.on_meet_time_to_client(spaceno, spaceid, new_delay)

		self._world_boss_stub.on_new_space(spaceno, spaceid, seq_num)

	def _on_tick_send_sep_line(self):
		''' 得到所有分线的信息 '''
		if not self.space_managers: return

		all_info = {spaceno : mgr.get_space_sep_line_info() for spaceno, mgr in self.space_managers.iteritems() if mgr.is_space_need_sep()}
		syn_info = {'ts' : DateTimeManager.now, 'info' : all_info}

		# 将额外的ctrl_info加到syn包中
		if self._need_to_push_ctrl_info:
			syn_info.update(self._need_to_push_ctrl_info)
			self._need_to_push_ctrl_info = {}

		# @TODO 使用这个方式比较效率不高, 也会增加当前game_manager的工作压力
		hex_api.global_message(space_consts.GMSG_EVENT_NAME_STUB_PUSH_SEP_LINE, syn_info)

	def _on_tick_world_boss(self):
		self._world_boss_stub.check_state()

	def setdefault_space_manager(self, spaceno):
		if spaceno not in self.space_managers:
			self.space_managers[spaceno] = SpaceManagerInStub(self, spaceno)
		return self.space_managers[spaceno]

	def is_valid_spaceno(self, spaceno):
		return G.spacem.is_valid_spaceno(spaceno)

	def call_applier_client(self, applier, method_name, para = {}, e_c = None, ctrl_info = {}):
		if not applier: return
		if para is None: para = {}
		if e_c is None: e_c = errcode.ERR_OK
		self.call(applier, method_name, self.mailbox, e_c, para, ctrl_info)

	def call_applier_enter_space_back(self, applier, para = {}, e_c = None, ctrl_info = {}):
		return self.call_applier_client(applier, 'rpc_from_stub_enter_space_back', para = para, e_c = e_c, ctrl_info = ctrl_info)

	@rpc_method(SERVER_ONLY, Int(), EntityID())
	def apply_create_space(self, spaceno, spaceid):
		if not self.is_valid_spaceno(spaceno): return
		self.setdefault_space_manager(spaceno).create_space(spaceid)

	@rpc_method(SERVER_ONLY, MailBox(), Int(), EntityID(), Dict(name = 'ctrl_info'))
	def apply_enter_space(self, applier, spaceno, spaceid, ctrl_info):
		if not self.is_valid_spaceno(spaceno):
			return self.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_INVALID_SPACENO, para = {"spaceno": spaceno}, ctrl_info = ctrl_info)
		self.setdefault_space_manager(spaceno).apply_enter_space(applier, spaceid, ctrl_info = ctrl_info)

	@rpc_method(SERVER_ONLY, MailBox(), Int(), EntityID(), EntityID(), Dict(name = 'ctrl_info'))
	def apply_create_and_enter_space(self, applier, spaceno, spaceid, teamid, ctrl_info):
		if not self.is_valid_spaceno(spaceno):
			return self.call_applier_enter_space_back(applier, e_c = errcode.ERR_ENTER_SPACE_OF_INVALID_SPACENO, para = {"spaceno": spaceno}, ctrl_info = ctrl_info)
		self.setdefault_space_manager(spaceno).apply_create_and_enter_space(applier, spaceid, teamid, ctrl_info = ctrl_info)

	@rpc_method(SERVER_ONLY, MailBox(), Int())
	def on_space_created(self, space, spaceno):
		if not self.is_valid_spaceno(spaceno): return
		self.setdefault_space_manager(spaceno).on_space_created(space)

	@rpc_method(SERVER_ONLY, EntityID(), Int())
	def on_space_destroy(self, spaceid, spaceno):
		if not self.is_valid_spaceno(spaceno): return
		self.setdefault_space_manager(spaceno).on_space_destroy(spaceid)

	@rpc_method(SERVER_ONLY, Dict(), Dict())
	def rpc_game_syn_space_sep_line_info_to_stub(self, syn_data, syn_ctrl_info):
		'''强制用汇报的信息写回当前分线信息'''
		time_to_client_dict = syn_ctrl_info.get('time_to_client', {})
		for spaceno, v in syn_data.iteritems():
			if not self.is_valid_spaceno(spaceno): continue
			self.setdefault_space_manager(spaceno).syn_space_sep_line_info(v, time_to_client_dict)

		# boss信息变化, 将当前所有boss的分线信息重新整理一遍
		if syn_ctrl_info.get('world_boss_info_change', False):
			self._on_world_boss_info_change()

	def _on_world_boss_info_change(self):
		# 需要通知所有人数据变化
		self._need_to_push_ctrl_info['syn_all_client'] = True

		# 通知立刻推送数据
		self.change_push_sep_line_next_delay(new_delay = 0)

	@rpc_method(SERVER_ONLY, Int(), Str(), List(), Dict())
	def rpc_call_all_space_method(self, spaceno, fun_name, args = None, kwargs = None):
		if not self.is_valid_spaceno(spaceno): return
		self.setdefault_space_manager(spaceno).call_all_space_method(fun_name, args, kwargs)

	@rpc_method(SERVER_ONLY, Int(), EntityID(), Str(), List(), Dict())
	def rpc_call_one_space_method(self, spaceno, spaceid, fun_name, args = None, kwargs = None):
		if not self.is_valid_spaceno(spaceno): return
		self.setdefault_space_manager(spaceno).call_one_space_method(spaceid, fun_name, args, kwargs)

	@rpc_method(SERVER_ONLY, Str(), List(), Dict())
	def rpc_call_every_space_method(self, fun_name, args = None, kwargs = None):
		for spacem in self.space_managers.itervalues():
			spacem.call_all_space_method(fun_name, args, kwargs)

	@rpc_method(SERVER_ONLY, MailBox(), EntityID(), Int(), EntityID())
	def rpc_get_spaceid_by_teamid(self, applier, teamid, spaceno, cb_id):
		spaceid = self.setdefault_space_manager(spaceno).get_spaceid_by_teamid(teamid)
		space_mailbox = self.setdefault_space_manager(spaceno).get_space_mailbox_by_spaceid(spaceid)
		self.call(applier, "rpc_get_spaceid_by_teamid_cb", space_mailbox, spaceid, cb_id)

	@rpc_method(SERVER_ONLY)
	def restart_world_boss(self):
		self._world_boss_stub.restart_world_boss()

	@rpc_method(SERVER_ONLY)
	def end_world_boss(self):
		self._world_boss_stub.end_world_boss()

	def create_space_and_call_init_func(self, spaceno, spaceid, *args, **kwargs):
		"""创建一个space, 并且创建成功之后, 调用其初始化代码, 通过`Space.init_func_from_init_controller`代码"""
		spacem = self.setdefault_space_manager(spaceno)
		if not spacem: return
		spacem.create_space_and_call_init_func(spaceid, *args, **kwargs)

	@rpc_method(SERVER_ONLY, EntityID())
	def rpc_on_team_dismissed(self, teamid):
		for space_manager in self.space_managers.itervalues():
			space_manager.try_destroy_space_when_team_is_dismissed(teamid)


