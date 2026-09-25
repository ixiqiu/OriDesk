"""APScheduler 定时任务：IMAP 轮询（开发文档 §8 / §9.1）。

运行：python manage.py runapscheduler

设计：
- 单进程 BlockingScheduler；每轮遍历启用中的邮箱，逐个同步。
- 邮件多时同步较慢是预期行为：一轮没跑完不会叠加下一轮（max_instances=1）。
- 邮箱之间互不影响；单个邮箱异常只记日志。
- 生产用 docker-compose 的 scheduler 容器单独运行本命令（仅一份实例）。
"""

from __future__ import annotations

import logging
import signal

from django.conf import settings
from django.core.management.base import BaseCommand
from django.utils import timezone

logger = logging.getLogger(__name__)


class Command(BaseCommand):
    help = "启动 APScheduler 定时任务（IMAP 轮询同步）"

    def add_arguments(self, parser):
        parser.add_argument(
            "--interval",
            type=int,
            default=None,
            help="轮询间隔秒数，默认取设置项 imap_poll_interval_seconds / IMAP_POLL_INTERVAL_SECONDS",
        )
        parser.add_argument(
            "--once",
            action="store_true",
            help="只同步一轮后退出（便于人工触发与巡检）",
        )

    def handle(self, *args, **options):
        from apscheduler.schedulers.blocking import BlockingScheduler
        from apscheduler.triggers.interval import IntervalTrigger

        from apps.audit.models import Setting
        from apps.mailboxes.sync import sync_all_mailboxes

        interval = options["interval"] or Setting.get_int(
            "imap_poll_interval_seconds",
            getattr(settings, "IMAP_POLL_INTERVAL_SECONDS", 60),
        )

        if options["once"]:
            results = sync_all_mailboxes()
            for item in results:
                self.stdout.write(str(item))
            return

        def job():
            started = timezone.now()
            logger.info("IMAP 轮询开始：%s", started.isoformat())
            try:
                results = sync_all_mailboxes()
            except Exception:  # noqa: BLE001 - 单轮失败不能杀死调度器
                logger.exception("IMAP 轮询出现未捕获异常。")
                return
            processed = sum(item.get("processed", 0) for item in results)
            logger.info(
                "IMAP 轮询结束：%s 个邮箱，入库 %s 封，耗时 %.1fs",
                len(results),
                processed,
                (timezone.now() - started).total_seconds(),
            )

        scheduler = BlockingScheduler(timezone=str(timezone.get_current_timezone()))
        scheduler.add_job(
            job,
            trigger=IntervalTrigger(seconds=max(10, interval)),
            id="imap_poll",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=max(10, interval),
            replace_existing=True,
        )

        def _shutdown(signum, frame):  # pragma: no cover - 需要真实信号
            logger.info("收到信号 %s，正在停止调度器…", signum)
            scheduler.shutdown(wait=False)

        signal.signal(signal.SIGTERM, _shutdown)
        signal.signal(signal.SIGINT, _shutdown)

        self.stdout.write(
            self.style.SUCCESS(f"APScheduler 已启动，IMAP 轮询间隔 {max(10, interval)} 秒。")
        )
        job()  # 启动即跑一轮，避免等待一个完整间隔
        scheduler.start()
