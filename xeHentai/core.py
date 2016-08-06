#!/usr/bin/env python
# coding:utf-8
# Contributor:
#      fffonion        <fffonion@gmail.com>

from __future__ import absolute_import
import os
import re
import sys
import math
import json
import time
from Queue import Queue, Empty
from .task import Task
from . import util
from . import proxy
from . import filters
from .rpc import RPCServer
from .i18n import i18n
from .util import logger
from .const import *
from .const import __version__
from .worker import *

sys.path.insert(1, FILEPATH)
try:
    import config
except ImportError:
    from . import config
sys.path.pop(1)

class xeHentai(object):
    def __init__(self):
        self.verstr = "%s%s" % (__version__, '-dev' if DEVELOPMENT else "")
        self.logger = logger.Logger()
        self._exit = False
        self.tasks = Queue() # for queueing, stores gid only
        self.last_task_guid = None
        self._all_tasks = {} # for saving states
        self._all_threads = [[] for i in range(20)]
        self.cfg = {k:v for k,v in config.__dict__.iteritems() if not k.startswith("_")}
        self.proxy = None
        self.cookies = {}
        self.headers = {
            'User-Agent': util.make_ua(),
            'Accept-Charset': 'utf-8;q=0.7,*;q=0.7',
            'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
            'Connection': 'keep-alive'
        }
        self.has_login = False
        self.load_session()
        self.rpc = None

    def update_config(self, cfg_dict):
        self.cfg.update({k:v for k, v in cfg_dict.iteritems() if k in cfg_dict})
        self.logger.set_level(logger.Logger.WARNING - self.cfg['log_verbose'])
        self.logger.verbose("cfg %s" % self.cfg)
        if cfg_dict['proxy']:
            if not self.proxy: # else we keep it None
                self.proxy = proxy.Pool()
            for p in self.cfg['proxy']:
                try:
                    self.proxy.add_proxy(p)
                except Exception as ex:
                    self.logger.warning(str(ex))
            self.logger.debug(i18n.PROXY_CANDIDATE_CNT % len(self.proxy.proxies))
        if not self.rpc and self.cfg['rpc_port'] and self.cfg['rpc_interface']:
            self.rpc = RPCServer(self, (self.cfg['rpc_interface'], int(self.cfg['rpc_port'])),
                secret = None if 'rpc_secret' not in self.cfg else self.cfg['rpc_secret'],
                logger = self.logger)
            self.rpc.start()
            self.logger.info(i18n.XEH_RPC_STARTED % (self.cfg['rpc_interface'], int(self.cfg['rpc_port'])))
        self.logger.set_logfile(self.cfg['log_path'])

    def _get_httpreq(self):
        return HttpReq(self.headers, logger = self.logger, proxy = self.proxy)

    def _get_httpworker(self, tid, task_q, flt, suc, fail, keep_alive):
        return HttpWorker(tid, task_q, flt, suc, fail,
            headers = self.headers, proxy = self.proxy,
            logger = self.logger, keep_alive = keep_alive)

    def add_task(self, url, cfg_dict = {}):
        url = url.strip()
        cfg = {k:v for k, v in self.cfg.iteritems() if k in (
            "dir", "download_ori", "download_thread_cnt", "scan_thread_cnt")}
        cfg.update(cfg_dict)
        if cfg['download_ori'] and not self.has_login:
            self.logger.warning(i18n.XEH_DOWNLOAD_ORI_NEED_LOGIN)
        t = Task(url, cfg)
        if t.guid in self._all_tasks:
            if self._all_tasks[t.guid].state in (TASK_STATE_FINISHED, TASK_STATE_FAILED):
                self.logger.debug(i18n.TASK_PUT_INTO_WAIT % t.guid)
                self._all_tasks[t.guid].state = TASK_STATE_WAITING
                self._all_tasks[t.guid].cleanup()
            return 0, t.guid
        self._all_tasks[t.guid] = t
        if not re.match("^https*://(g\.e\-|ex)hentai\.org/[^/]+/\d+/[^/]+/*$", url):
            t.set_fail(ERR_URL_NOT_RECOGNIZED)
        elif not self.has_login and re.match("^https*://exhentai\.org", url):
            t.set_fail(ERR_CANT_DOWNLOAD_EXH)
        else:
            self.tasks.put(t.guid)
            return 0, t.guid
        self.logger.error(i18n.TASK_ERROR % (t.guid, i18n.c(t.failcode)))
        return t.failcode, None

    def del_task(self, guid):
        if guid not in self._all_tasks:
            return ERR_TASK_NOT_FOUND, None
        if TASK_STATE_FAILED< self._all_tasks[guid].state < TASK_STATE_FINISHED:
            return ERR_DELETE_RUNNING_TASK, None
        del self._all_tasks[guid]
        return ERR_NO_ERROR, ""

    def pause_task(self, guid):
        if guid not in self._all_tasks:
            return ERR_TASK_NOT_FOUND, None
        t = self._all_tasks[guid]
        if t.state in (TASK_STATE_PAUSED, TASK_STATE_FINISHED, TASK_STATE_FAILED):
            return ERR_TASK_CANNOT_PAUSE, None
        if t._monitor:
            t._monitor._exit = lambda x: True
        t.state = TASK_STATE_PAUSED
        return ERR_NO_ERROR, ""

    def resume_task(self, guid):
        if guid not in self._all_tasks:
            return ERR_TASK_NOT_FOUND, None
        t = self._all_tasks[guid]
        if TASK_STATE_FAILED< t.state < TASK_STATE_FINISHED:
            return ERR_TASK_CANNOT_RESUME, None
        t.state = max(t.state, TASK_STATE_WAITING)
        return ERR_NO_ERROR, ""

    def list_tasks(self, level = "download"):
        level = "TASK_STATE_%s" % level.upper()
        if level not in globals():
            return ERR_TASK_LEVEL_UNDEF, None
        lv = globals()[level]
        rt = {k:v.to_dict() for k, v in self._all_tasks.iteritems() if v.state == lv}
        return ERR_NO_ERROR, rt

    def _do_task(self, task_guid):
        task = self._all_tasks[task_guid]
        if task.state == TASK_STATE_WAITING:
            task.state = TASK_STATE_GET_META
        req = self._get_httpreq()
        if not task.page_q:
            task.page_q = Queue() # per image page queue
        if not task.img_q:
            task.img_q = Queue() # (image url, savepath) queue
        monitor_started = False
        while self._exit < XEH_STATE_FULL_EXIT:
            # wait for threads from former task to stop
            if self._all_threads[task.state]:
                self.logger.verbose("wait %d threads in state %s" % (
                    len(self._all_threads[task.state]), task.state))
                for t in self._all_threads[task.state]:
                    t.join()
                self._all_threads[task.state] = []
                # check again before we bring up new threads
                continue
            if task.state >= TASK_STATE_SCAN_IMG and not monitor_started:
                self.logger.verbose("state %d >= %d, bring up montior" % (task.state, TASK_STATE_SCAN_IMG))
                # bring up the monitor here, ahead of workers
                mon = Monitor(req, self.proxy, self.logger, task)
                _ = ['down-%d' % (i + 1) for i in range(task.config['download_thread_cnt'])]
                # if we jumpstart from a saved session to DOQNLOAD
                # there will be no scan_thread
                # if task.state >= TASK_STATE_SCAN_PAGE:
                #    _ += ['list-1']
                if task.state >= TASK_STATE_SCAN_IMG:
                    _ += ['scan-%d' % (i + 1) for i in range(task.config['scan_thread_cnt'])]
                mon.set_vote_ns(_)
                self._monitor = mon
                mon.start()
                # put in the lowest state
                self._all_threads[TASK_STATE_SCAN_IMG].append(mon)
                monitor_started = True

            if task.state == TASK_STATE_GET_META: # grab meta data
                r = req.request("GET", task.url,
                    filters.flt_metadata,
                    lambda x:(task.meta.update(x),
                        self.logger.info(i18n.TASK_TITLE % (
                            task_guid, task.meta['gjname'] or task.meta['gnname']))),
                    lambda x:task.set_fail(x))
                if r in (ERR_ONLY_VISIBLE_EXH, ERR_GALLERY_REMOVED) and self.has_login and \
                        task.migrate_exhentai():
                    self.logger.info(i18n.TASK_MIGRATE_EXH % task_guid)
                    self.tasks.put(task_guid)
                    break
            # elif task.state == TASK_STATE_GET_HATHDL: # download hathdl
            #     r = req.request("GET",
            #         "%s/hathdler.php?gid=%s&t=%s" % (task.base_url(), task.gid, task.sethash),
            #         filters.flt_hathdl,
            #         lambda x:(task.meta.update(x),
            #             task.guess_ori(),
            #             task.scan_downloaded()),
            #                 #task.meta['has_ori'] and task.config['download_ori'])),
            #         lambda x:task.set_fail(x),)
            #     self.logger.info(i18n.TASK_WILL_DOWNLOAD_CNT % (
            #         task_guid, task.meta['total'] - len(task._flist_done),
            #         task.meta['total']))
            elif task.state == TASK_STATE_SCAN_PAGE:
                # if task.config['fast_scan'] and not task.has_ori:
                #     self.logger.info(i18n.TASK_FAST_SCAN % task.guid)
                #     for p in task.meta['filelist']:
                #         task.queue_wrapper(task.page_q.put, pichash = p)
                # else:
                # scan by our own, should not be here currently
                # start backup thread
                for x in range(0, int(math.ceil(task.meta['total'] / 40.0))):
                    r = req.request("GET",
                        "%s/?p=%d" % (task.url, x),
                        filters.flt_pageurl,
                        lambda x: task.queue_wrapper(task.page_q.put, url = x),
                        lambda x: task.set_fail(x))
                    if task.failcode:
                        break
                if not task.failcode:
                    task.scan_downloaded()
                    self.logger.info(i18n.TASK_WILL_DOWNLOAD_CNT % (
                        task_guid, task.meta['total'] - len(task._flist_done),
                        task.meta['total']))
            elif task.state == TASK_STATE_SCAN_IMG:
                # spawn thread to scan images
                for i in range(task.config['scan_thread_cnt']):
                    tid = 'scan-%d' % (i + 1)
                    _ = self._get_httpworker(tid, task.page_q,
                        filters.flt_imgurl_wrapper(task.config['download_ori'] and self.has_login),
                        lambda x, tid = tid: (task.img_q.put(x[0]),
                            task.set_reload_url(x[0], x[1], x[2]),
                            mon.vote(tid, 0)),
                        lambda x, tid = tid: (mon.vote(tid, x)),
                        mon.wrk_keepalive)
                    # _._exit = lambda t: t._finish_queue()
                    _.start()
                    self._all_threads[TASK_STATE_SCAN_IMG].append(_)
                task.state = TASK_STATE_DOWNLOAD - 1
            elif task.state == TASK_STATE_SCAN_ARCHIVE:
                task.state = TASK_STATE_DOWNLOAD - 1
            elif task.state == TASK_STATE_DOWNLOAD:
                # spawn thread to download all urls
                for i in range(task.config['download_thread_cnt']):
                    tid = 'down-%d' % (i + 1)
                    _ = self._get_httpworker(tid, task.img_q,
                        filters.download_file_wrapper(task.config['dir']),
                        lambda x, tid = tid: (task.save_file(x[1], x[0]),
                            self.logger.debug(i18n.XEH_FILE_DOWNLOADED % (task.get_fname(x[1]))),
                            mon.vote(tid, 0)),
                        lambda x, tid = tid: (
                            task.page_q.put(task.get_reload_url(x[1])),# if x[0] != ERR_QUOTA_EXCEEDED else None,
                            mon.vote(tid, x[0])),
                        mon.wrk_keepalive)
                    _.start()
                    self._all_threads[TASK_STATE_DOWNLOAD].append(_)

                # break current task loop
                break

            if task.failcode:
                self.logger.error(i18n.TASK_ERROR % (task_guid, i18n.c(task.failcode)))
                # wait all threads to finish
                break
            else:
                task.state += 1

    def _task_loop(self):
        task_guid = None
        cnt = 0
        while not self._exit:
            # get a new task
            if cnt == 10:
                self.save_session()
                cnt = 0
            try:
                _ = self.tasks.get(False)
                self.last_task_guid = task_guid
                task_guid = _
            except Empty:
                time.sleep(1)
                cnt += 1
                continue
            else:
                task = self._all_tasks[task_guid]
                if TASK_STATE_PAUSED < task.state < TASK_STATE_FINISHED:
                    self.logger.info(i18n.TASK_START % task_guid)
                    self.save_session()
                    cnt = 0
                    self._do_task(task_guid)
        self.logger.info(i18n.XEH_LOOP_FINISHED)
        self._cleanup()

    def _term_threads(self):
        self._exit = XEH_STATE_FULL_EXIT
        for l in self._all_threads:
            for p in l:
                p._exit = lambda x:True

    def _cleanup(self):
        self._exit = self._exit if self._exit > 0 else XEH_STATE_SOFT_EXIT
        self.save_session()
        self._join_all()
        # save it again in case we miss something
        self.save_session()
        self.logger.cleanup()
        # let's send a request to rpc server to unblock it
        if self.rpc:
            self.rpc._exit = lambda x:True
            import requests
            requests.get("http://%s:%s/" % (self.cfg['rpc_interface'], self.cfg['rpc_port']))
            self.rpc.join()
        self._exit = XEH_STATE_CLEAN

    def _join_all(self):
        for l in self._all_threads:
            for p in l:
                p.join()

    def save_session(self):
        with open("h.json", "w") as f:
            try:
                f.write(json.dumps({
                    'tasks':{k: v.to_dict() for k,v in self._all_tasks.iteritems()},
                    'cookies':self.cookies}))
            except Exception as ex:
                self.logger.warning(i18n.SESSION_LOAD_EXCEPTION % ex)
                return ERR_SAVE_SESSION_FAILED, str(ex)
        return ERR_NO_ERROR, None

    def load_session(self):
        if not os.path.exists("h.json"):
            return
        with open("h.json") as f:
            try:
                j = json.loads(f.read())
            except Exception as ex:
                self.logger.warning(i18n.SESSION_SAVE_EXCEPTION % ex)
                return ERR_SAVE_SESSION_FAILED, str(ex)
            else:
                for _ in j['tasks'].values():
                    _t = Task("", {}).from_dict(_)
                    if 'filelist' in _t.meta:
                        _t.scan_downloaded()
                            #_t.meta['has_ori'] and task.config['download_ori'])
                    # since we don't block on scan_img state, an unempty page_q
                    # indicates we should start from scan_img state,
                    if _t.state == TASK_STATE_DOWNLOAD and _t.page_q:
                        _t.state = TASK_STATE_SCAN_IMG
                    self._all_tasks[_['guid']] = _t
                    self.tasks.put(_['guid'])
                self.logger.info(i18n.XEH_LOAD_TASKS_CNT % len(self._all_tasks))
                self.cookies = j['cookies']
                if self.cookies:
                    self.headers.update({'Cookie':util.make_cookie(self.cookies)})
                    self.has_login = True
        return ERR_NO_ERROR, None

    def login_exhentai(self, name, pwd):
        if 'ipb_member_id' in self.cookies and 'ipb_pass_hash' in self.cookies:
            return
        self.logger.debug(i18n.XEH_LOGIN_EXHENTAI)
        logindata = {
            'UserName':name,
            'returntype':'8',
            'CookieDate':'1',
            'b':'d',
            'bt':'pone',
            'PassWord':pwd
        }
        req = self._get_httpreq()
        req.request("POST", "http://forums.e-hentai.org/index.php?act=Login&CODE=01",
            filters.login_exhentai,
            lambda x:(
                setattr(self, 'cookies', x),
                setattr(self, 'has_login', True),
                self.headers.update({'Cookie':util.make_cookie(self.cookies)}),
                self.save_session(),
                self.logger.info(i18n.XEH_LOGIN_OK)),
            lambda x:(self.logger.warning(x),
                self.logger.info(i18n.XEH_LOGIN_FAILED)),
            logindata)
        return ERR_NO_ERROR, self.has_login

    def set_cookie(self, cookie):
        self.cookies.update(util.parse_cookie(cookie))
        self.headers.update({'Cookie':util.make_cookie(self.cookies)})
        self.has_login = True
        return ERR_NO_ERROR, None


if __name__ == '__main__':
    pass
