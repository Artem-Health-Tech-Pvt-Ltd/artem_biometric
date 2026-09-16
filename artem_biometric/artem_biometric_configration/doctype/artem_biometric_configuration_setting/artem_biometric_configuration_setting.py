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
	

class ArtemBiometricConfigurationSetting(Document):
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
    if batch_size <= 0:
        batch_size = 100

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

            last_punch_time = None

            while True:
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
                """
                params = [from_date, to_date, serial_no]

                if last_punch_time:
                    query += " AND punch_time > %s"
                    params.append(last_punch_time)

                query += " ORDER BY punch_time ASC LIMIT %s"
                params.append(batch_size)

                with connection.cursor() as cursor:
                    cursor.execute(query, tuple(params))
                    records = cursor.fetchall()

                if not records:
                    break

                for record in records:
                    punch_time = record.get("punch_time")
                    if punch_time:
                        last_punch_time = punch_time

                    try:
                        success, err_type = process_biometric_record(
                            connection, record, error_collector=error_collector
                        )

                        if not success:
                            if err_type == "Duplicate Employee Checkin":
                                mark_record_synced(connection, record)
                            total_failed += 1
                            continue

                        # Employee Checkin was successfully created
                        frappe.db.commit() # nosemgrap

                        # Only mark biometric record as synced when checkin is created
                        mark_record_synced(connection, record)

                        total_processed += 1

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

                if len(records) < batch_size:
                    break

        # Log consolidated errors grouped by error category
        log_consolidated_errors(error_collector)

        frappe.db.commit() # nosemgrap

        already_exist = False
        if total_processed == 0:
            if error_collector.get("Duplicate Employee Checkin") or frappe.db.exists(
                "Employee Checkin",
                {
                    "time": ["between", [f"{from_date} 00:00:00", f"{to_date} 23:59:59"]]
                },
            ):
                already_exist = True
                message = _("Already exist employees punch in employee checkin for select date")
            else:
                message = _("{0} punch records processed successfully, {1} failed.").format(
                    total_processed, total_failed
                )
        else:
            message = _("{0} punch records processed successfully, {1} failed.").format(
                total_processed, total_failed
            )

        return {
            "success": True,
            "already_exist": already_exist,
            "processed": total_processed,
            "failed": total_failed,
            "message": message,
        }

    finally:
        if connection:
            connection.close()