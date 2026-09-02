# Date : 01st September 2026
# Author : Anjali Patoliya
# Purpose : Biometric API for fetching employee checkin data from biometric device mysql database and creating Employee Checkin records in ERPNext.


from multiprocessing import connection

import frappe
import pymysql
from frappe import _


SETTINGS_DOCTYPE = "Artem Biometric Configration Setting"
SOURCE_TABLE = "att_data"


def get_biometric_settings():
    """Get biometric configuration from Single DocType."""

    settings = frappe.get_single(SETTINGS_DOCTYPE)

    if not settings.enable:
        frappe.throw(_("Biometric synchronization is disabled."))

    return settings


def get_mysql_connection():
    """Create connection with biometric MySQL database."""

    settings = get_biometric_settings()

    return pymysql.connect(
        host=settings.db_hostip,
        user=settings.db_username,
        password=settings.get_password("db_password"),
        database=settings.db_name,
        charset="utf8mb4",
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=True,
    )
    

def get_employee(emp_code):
    """Find Employee using biometric attendance device ID."""

    if not emp_code:
        return None

    emp_code_str = str(emp_code).strip()

    # 1. Match attendance_device_id
    employee = frappe.db.get_value(
        "Employee",
        {
            "attendance_device_id": emp_code_str,
            "status": "Active",
        },
        ["name", "company"],
        as_dict=True,
    )

    # 2. If not found and emp_code_str is numeric, try without leading zeros
    if not employee and emp_code_str.isdigit():
        employee = frappe.db.get_value(
            "Employee",
            {
                "attendance_device_id": str(int(emp_code_str)),
                "status": "Active",
            },
            ["name", "company"],
            as_dict=True,
        )


    return employee


def check_duplicate_employee_checkin(employee, punch_time):
    """Check whether Employee Checkin already exists."""

    return frappe.db.exists(
        "Employee Checkin",
        {
            "employee": employee,
            "time": punch_time,
        },
    )


def create_employee_checkin(record, employee):
    """Create Employee Checkin from biometric record."""

    punch_state = str(record.get("punch_state") or "").strip().lower()

    punch_state_clean = " ".join(punch_state.replace("-", " ").replace("_", " ").split())

    log_type = None

    if punch_state in ("0", "in", "checkin", "i", "c/in") or punch_state_clean in ("in", "check in") or "check in" in punch_state:
        log_type = "IN"
    elif punch_state in ("1", "out", "checkout", "o", "c/out") or punch_state_clean in ("out", "check out") or "check out" in punch_state:
        log_type = "OUT"

    device_id = record.get("terminal_sn")

    checkin_doc = {
        "doctype": "Employee Checkin",
        "employee": employee,
        "time": record["punch_time"],
        "log_type": log_type,
        "device_id": device_id,
    }

    if device_id:
        settings = frappe.get_cached_doc(SETTINGS_DOCTYPE)
        if settings.default_latitude:
            checkin_doc["latitude"] = settings.default_latitude
        if settings.default_longitude:
            checkin_doc["longitude"] = settings.default_longitude

    checkin = frappe.get_doc(checkin_doc)

    checkin.insert(ignore_permissions=True)

    return checkin


def process_biometric_record(connection, record, error_collector=None):
	"""Process a single biometric punch record."""

	emp_code = record.get("emp_code")
	punch_time = record.get("punch_time")
	terminal_sn = record.get("terminal_sn")
	terminal_alias = record.get("terminal_alias")

	try:
		# Validate biometric employee ID
		if not emp_code:
			if error_collector is not None:
				error_collector.setdefault("Employee Not Found", []).append({
					"emp_code": emp_code,
					"punch_time": str(punch_time),
					"terminal_sn": terminal_sn,
					"terminal_alias": terminal_alias,
					"error": "Employee biometric ID is missing.",
				})
			return False, "Employee Not Found"

		# Validate punch time
		if not punch_time:
			if error_collector is not None:
				error_collector.setdefault("Invalid Punch Data", []).append({
					"emp_code": emp_code,
					"punch_time": str(punch_time),
					"terminal_sn": terminal_sn,
					"terminal_alias": terminal_alias,
					"error": "Punch time is missing.",
				})
			return False, "Invalid Punch Data"

		# Find employee using biometric/device ID
		employee = get_employee(emp_code)

		if not employee:
			if error_collector is not None:
				error_collector.setdefault("Employee Not Found", []).append({
					"emp_code": emp_code,
					"punch_time": str(punch_time),
					"terminal_sn": terminal_sn,
					"terminal_alias": terminal_alias,
					"error": f"Employee not found for Attendance Device ID: {emp_code}",
				})
			return False, "Employee Not Found"

		# Check whether Employee Checkin already exists
		if check_duplicate_employee_checkin(
			employee.name,
			punch_time
		):
			frappe.logger().info(
				f"Duplicate Employee Checkin found for "
				f"{employee.name} at {punch_time}. "
				f"Marking biometric record as processed."
			)

			if error_collector is not None:
				error_collector.setdefault("Duplicate Employee Checkin", []).append({
					"emp_code": emp_code,
					"employee": employee.name,
					"punch_time": str(punch_time),
					"terminal_sn": terminal_sn,
					"terminal_alias": terminal_alias,
					"note": f"Checkin already exists for {employee.name} at {punch_time}",
				})

			return True, "Duplicate Employee Checkin"

		# Create Employee Checkin
		checkin = create_employee_checkin(
			record,
			employee.name
		)

		# Make sure Employee Checkin was actually created
		if not checkin or not checkin.name:
			raise ValueError(
				f"Employee Checkin was not created for "
				f"{employee.name} at {punch_time}"
			)

		frappe.logger().info(
			f"Employee Checkin created successfully: "
			f"{checkin.name} for {employee.name} at {punch_time}"
		)

		return True, None

	except Exception as error:
		# Rollback only the failed transaction
		frappe.db.rollback()

		if error_collector is not None:
			error_collector.setdefault("Checkin Creation Error", []).append({
				"emp_code": emp_code,
				"employee": employee.name if "employee" in locals() and employee else "Unknown",
				"punch_time": str(punch_time),
				"terminal_sn": terminal_sn,
				"terminal_alias": terminal_alias,
				"error": str(error),
			})
		else:
			log_biometric_error(record, error)

		return False, "Checkin Creation Error"


def mark_record_synced(connection, record):
    """Mark biometric record as synchronized."""

    query = """
        UPDATE att_data
        SET sync = 1
        WHERE emp_code = %s
          AND punch_time = %s
          AND terminal_sn = %s
          AND sync = 0
        LIMIT 1
    """

    with connection.cursor() as cursor:
        cursor.execute(
            query,
            (
                record.get("emp_code"),
                record.get("punch_time"),
                record.get("terminal_sn"),
            ),
        )


def log_biometric_error(record, error):
    """Log individual biometric synchronization error."""

    frappe.log_error(
        title="Biometric Punch Synchronization Error",
        message=frappe.as_json({
            "emp_code": record.get("emp_code"),
            "punch_time": str(record.get("punch_time")),
            "terminal_sn": record.get("terminal_sn"),
            "terminal_alias": record.get("terminal_alias"),
            "error": str(error),
        }),
    )


def log_consolidated_errors(error_collector):
    """Log grouped errors into a single Error Log entry per error type."""

    if not error_collector:
        return

    for error_type, errors in error_collector.items():
        if not errors:
            continue

        title = f"Biometric Sync - {error_type} ({len(errors)} records)"
        message = (
            f"Error Category: {error_type}\n"
            f"Total Affected Records: {len(errors)}\n\n"
            f"Details:\n"
            f"{frappe.as_json(errors, indent=2)}"
        )

        frappe.log_error(
            title=title,
            message=message,
        )


def sync_device(device, connection=None, error_collector=None):
    """Synchronize punches for one biometric device."""

    should_close_conn = False
    if connection is None:
        connection = get_mysql_connection()
        should_close_conn = True

    try:
        settings = get_biometric_settings()
        batch_size = int(settings.records_per_batch or 100)

        last_punch_log = device.last_punch_log
        serial_no = (device.biometric_device_id_serial_no or "").strip()

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
            WHERE sync = 0
        """

        params = []

        if serial_no:
            query += " AND terminal_sn = %s"
            params.append(serial_no)

        if last_punch_log:
            query += " AND punch_time > %s"
            params.append(last_punch_log)

        query += " ORDER BY punch_time ASC LIMIT %s"
        params.append(batch_size)

        with connection.cursor() as cursor:
            cursor.execute(query, params)
            records = cursor.fetchall()

        if not records:
            return {
                "processed": 0,
                "failed": 0,
            }

        processed = 0
        failed = 0
        latest_successful_punch = last_punch_log

        for record in records:
            try:
                success, _err_type = process_biometric_record(
                    connection, record, error_collector=error_collector
                )

                if not success:
                    failed += 1
                    continue

                # Employee Checkin was successfully created
                frappe.db.commit() # nosemgrap

                # Only now mark biometric record as synced
                mark_record_synced(connection, record)

                processed += 1

                punch_time = record.get("punch_time")
                if punch_time:
                    punch_dt = frappe.utils.get_datetime(punch_time)
                    if not latest_successful_punch or punch_dt > frappe.utils.get_datetime(latest_successful_punch):
                        latest_successful_punch = punch_dt

            except Exception as error:
                failed += 1

                frappe.db.rollback()

                if error_collector is not None:
                    error_collector.setdefault("Checkin Creation Error", []).append({
                        "emp_code": record.get("emp_code"),
                        "punch_time": str(record.get("punch_time")),
                        "terminal_sn": record.get("terminal_sn"),
                        "terminal_alias": record.get("terminal_alias"),
                        "error": str(error),
                    })
                else:
                    log_biometric_error(record, error)

        # Update checkpoint in child table if new punches were processed
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

        return {
            "processed": processed,
            "failed": failed,
            "last_punch_log": latest_successful_punch,
        }

    finally:
        if should_close_conn and connection:
            connection.close()


def sync_biometric_attendance():
    """Scheduled biometric synchronization."""

    settings = frappe.get_single(SETTINGS_DOCTYPE)

    if not settings.enable:
        return

    total_processed = 0
    total_failed = 0

    error_collector = {
        "Employee Not Found": [],
        "Duplicate Employee Checkin": [],
        "Invalid Punch Data": [],
        "Checkin Creation Error": [],
    }

    connection = None
    try:
        connection = get_mysql_connection()

        for device in settings.artem_biometric_device_details:
            result = sync_device(
                device,
                connection=connection,
                error_collector=error_collector
            )

            total_processed += result["processed"]
            total_failed += result["failed"]

        # Commit child table updates
        frappe.db.commit() # nosemgrap

        # Log consolidated errors grouped by type
        log_consolidated_errors(error_collector)

        frappe.logger("biometric_sync").info(
            f"Biometric sync completed. "
            f"Processed: {total_processed}, "
            f"Failed: {total_failed}"
        )

    finally:
        if connection:
            connection.close()