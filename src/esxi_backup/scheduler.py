from __future__ import annotations

import logging
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from .models import BackupSchedule, SchedulePolicy
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
        policies = self.service.repository.list_schedule_policies()
        if not policies:
            for legacy in self.service.repository.list_schedules():
                policy = SchedulePolicy(
                    id=f"legacy-{legacy.vm_id}", name=f"{legacy.vm_name} backup",
                    vm_ids=[legacy.vm_id], frequency=legacy.frequency,
                    hour=legacy.hour, minute=legacy.minute, weekday=legacy.weekday,
                    quiesce=self.service.config.quiesce,
                )
                self.service.repository.save_schedule_policy(policy)
                policies.append(policy)
        for schedule in policies:
            self.apply_policy(schedule)

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

    def apply_policy(self, schedule: SchedulePolicy) -> SchedulePolicy:
        job_id = f"schedule:{schedule.id}"
        existing = self.scheduler.get_job(job_id)
        if existing:
            self.scheduler.remove_job(job_id)
        self.service.repository.save_schedule_policy(schedule)
        if schedule.frequency == "disabled" or not schedule.vm_ids:
            return schedule
        trigger = CronTrigger(
            hour=schedule.hour, minute=schedule.minute,
            day_of_week=schedule.weekday if schedule.frequency == "weekly" else None,
            timezone=self.scheduler.timezone,
        )
        job = self.scheduler.add_job(
            self._run_policy, trigger=trigger, args=[schedule], id=job_id,
            name=schedule.name, replace_existing=True, max_instances=1,
            coalesce=True, misfire_grace_time=3600,
        )
        return schedule.model_copy(update={"next_run_at": job.next_run_time})

    def policies(self) -> list[SchedulePolicy]:
        output = []
        for schedule in self.service.repository.list_schedule_policies():
            job = self.scheduler.get_job(f"schedule:{schedule.id}")
            output.append(schedule.model_copy(update={
                "next_run_at": job.next_run_time if job else None
            }))
        return output

    def delete_policy(self, schedule_id: str) -> None:
        job = self.scheduler.get_job(f"schedule:{schedule_id}")
        if job:
            self.scheduler.remove_job(job.id)
        self.service.repository.delete_schedule_policy(schedule_id)

    def _run_policy(self, schedule: SchedulePolicy) -> None:
        if error := self.service.repository.availability_error():
            log.error("Skipping schedule %s: %s", schedule.name, error)
            return
        for vm_id in schedule.vm_ids:
            try:
                record = self.service.backup(vm_id, quiesce=schedule.quiesce)
                if schedule.build_ova and self.service.supports_ova(record.id):
                    self.service.export_ova_for_web(record.id)
            except Exception:
                log.exception("Scheduled backup failed for %s in %s", vm_id, schedule.name)

    def _run_backup(self, vm_id: str) -> None:
        if error := self.service.repository.availability_error():
            log.error("Skipping scheduled backup for %s: %s", vm_id, error)
            return
        try:
            self.service.backup(vm_id)
        except Exception:
            log.exception("Scheduled backup failed for %s", vm_id)
