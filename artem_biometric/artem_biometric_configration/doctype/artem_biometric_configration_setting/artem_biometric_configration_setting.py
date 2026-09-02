# Copyright (c) 2026, Artem Healthtech and contributors
# For license information, please see license.txt


# Date: 01-09-2026
# Author: Anjali Patoliya

import frappe
from frappe import _

from frappe.model.document import Document
from artem_biometric.api.biometric import (
	SOURCE_TABLE,
	get_biometric_settings,
	get_mysql_connection,
	process_biometric_record,
	mark_record_synced,
	log_consolidated_errors
)
	

class ArtemBiometricConfigrationSetting(Document):
	pass



# Manually fetch biometric punches for selected date range.
@frappe.whitelist()
def fetch_employee_punch_data(from_date: str, to_date: str) -> dict:
    """Manually fetch biometric punches for selected date range."""

    if not from_date or not to_date:
        frappe.throw(_("Please select From Date and To Date."))

    if from_date > to_date:
        frappe.throw(_("From Date cannot be greater than To Date."))

    settings = get_biometric_settings()

    batch_size = int(settings.records_per_batch or 100)

    connection = None

    error_collector = {
        "Employee Not Found": [],
        "Duplicate Employee Checkin": [],
        "Invalid Punch Data": [],
        "Checkin Creation Error": [],
    }

    try:
        connection = get_mysql_connection()

        total_processed = 0
        total_failed = 0

        for device in settings.artem_biometric_device_details:
            serial_no = (device.biometric_device_id_serial_no or "").strip()
            if not serial_no:
                continue

            query = f"""
                SELECT
                    emp_code,
                    punch_time,
                    punch_state,
                    terminal_sn,
                    terminal_alias,
                    upload_time,
                    sync
                FROM {SOURCE_TABLE}
                WHERE punch_time >= %s
                  AND punch_time < DATE_ADD(%s, INTERVAL 1 DAY)
                  AND terminal_sn = %s
                  AND sync = 0
                ORDER BY punch_time ASC
                LIMIT %s
            """

            with connection.cursor() as cursor:
                cursor.execute(
                    query,
                    (
                        from_date,
                        to_date,
                        serial_no,
                        batch_size,
                    ),
                )
                records = cursor.fetchall()

            latest_successful_punch = device.last_punch_log

            for record in records:
                try:
                    success, _err_type = process_biometric_record(
                        connection, record, error_collector=error_collector
                    )

                    if not success:
                        total_failed += 1
                        continue

                    # Employee Checkin was successfully created
                    frappe.db.commit() # nosemgrap

                    # Only mark biometric record as synced when checkin is created
                    mark_record_synced(connection, record)

                    total_processed += 1

                    punch_time = record.get("punch_time")
                    if punch_time:
                        punch_dt = frappe.utils.get_datetime(punch_time)
                        if not latest_successful_punch or punch_dt > frappe.utils.get_datetime(latest_successful_punch):
                            latest_successful_punch = punch_dt

                except Exception as error:
                    total_failed += 1
                    frappe.db.rollback()
                    error_collector["Checkin Creation Error"].append({
                        "emp_code": record.get("emp_code"),
                        "punch_time": str(record.get("punch_time")),
                        "terminal_sn": record.get("terminal_sn"),
                        "terminal_alias": record.get("terminal_alias"),
                        "error": str(error),
                    })

            # Update last_punch_log for device in child table
            if latest_successful_punch and (
                not device.last_punch_log
                or frappe.utils.get_datetime(latest_successful_punch) > frappe.utils.get_datetime(device.last_punch_log)
            ):
                device.last_punch_log = latest_successful_punch
                if device.name:
                    frappe.db.set_value(
                        "Biometric Device Details",
                        device.name,
                        "last_punch_log",
                        latest_successful_punch,
                        update_modified=False,
                    )

        # Commit child table updates
        frappe.db.commit() # nosemgrap

        # Log consolidated errors grouped by error category
        log_consolidated_errors(error_collector)

        return {
            "success": True,
            "processed": total_processed,
            "failed": total_failed,
            "message": _("{0} punch records processed successfully, {1} failed.").format(
                total_processed, total_failed
            ),
        }

    finally:

        if connection:
            connection.close()