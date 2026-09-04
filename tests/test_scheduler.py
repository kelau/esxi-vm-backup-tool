from esxi_backup.models import AppConfig, BackupSchedule, ServerConfig
from esxi_backup.scheduler import BackupScheduler
from esxi_backup.service import BackupService


def test_schedule_persists_and_reloads_with_next_run(tmp_path):
    service = BackupService(AppConfig(
        server=ServerConfig(host="host", username="user", password="secret"),
        repository=str(tmp_path),
    ))
    scheduler = BackupScheduler(service)
    scheduler.start()
    try:
        saved = scheduler.apply(BackupSchedule(
            vm_id="vm-1", vm_name="database", frequency="weekly",
            weekday=6, hour=3, minute=15,
        ))
        assert saved.next_run_at is not None
        assert scheduler.schedules()[0].frequency == "weekly"
        assert service.repository.get_schedule("vm-1").weekday == 6
    finally:
        scheduler.shutdown()


def test_disabling_schedule_removes_job(tmp_path):
    service = BackupService(AppConfig(
        server=ServerConfig(host="host", username="user", password="secret"),
        repository=str(tmp_path),
    ))
    scheduler = BackupScheduler(service)
    scheduler.start()
    try:
        scheduler.apply(BackupSchedule(
            vm_id="vm-1", vm_name="database", frequency="daily", hour=1,
        ))
        scheduler.apply(BackupSchedule(
            vm_id="vm-1", vm_name="database", frequency="disabled",
        ))
        assert scheduler.scheduler.get_job("vm-backup:vm-1") is None
        assert scheduler.schedules()[0].next_run_at is None
    finally:
        scheduler.shutdown()
