from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .models import BackupSchedule
from .service import BackupService

log = logging.getLogger(__name__)


class BackupScheduler:
    def __init__(self, service: BackupService, timezone: str = "local"):
        self.service = service
        tz = None if timezone == "local" else ZoneInfo(timezone)
        self.scheduler = BackgroundScheduler(timezone=tz)

    def start(self) -> None:
        if not self.scheduler.running:
            self.scheduler.start()
        for schedule in self.service.repository.list_schedules():
            self.apply(schedule)

    def shutdown(self) -> None:
        if self.scheduler.running:
            self.scheduler.shutdown(wait=False)

    def apply(self, schedule: BackupSchedule) -> BackupSchedule:
        job_id = f"vm-backup:{schedule.vm_id}"
        existing = self.scheduler.get_job(job_id)
        if existing:
            self.scheduler.remove_job(job_id)
        self.service.repository.save_schedule(schedule)
        if schedule.frequency == "disabled":
            return schedule
        day_of_week = schedule.weekday if schedule.frequency == "weekly" else None
        trigger = CronTrigger(
            hour=schedule.hour, minute=schedule.minute, day_of_week=day_of_week,
            timezone=self.scheduler.timezone,
        )
        job = self.scheduler.add_job(
            self._run_backup, trigger=trigger, args=[schedule.vm_id], id=job_id,
            name=f"Backup {schedule.vm_name}", replace_existing=True,
            max_instances=1, coalesce=True, misfire_grace_time=3600,
        )
        return schedule.model_copy(update={"next_run_at": job.next_run_time})

    def schedules(self) -> list[BackupSchedule]:
        output = []
        for schedule in self.service.repository.list_schedules():
            job = self.scheduler.get_job(f"vm-backup:{schedule.vm_id}")
            output.append(schedule.model_copy(update={
                "next_run_at": job.next_run_time if job else None
            }))
        return output

    def _run_backup(self, vm_id: str) -> None:
        try:
            self.service.backup(vm_id)
        except Exception:
            log.exception("Scheduled backup failed for %s", vm_id)
