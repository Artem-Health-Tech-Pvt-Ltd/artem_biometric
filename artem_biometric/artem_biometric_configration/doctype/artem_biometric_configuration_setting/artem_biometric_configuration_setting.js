// Copyright (c) 2026, Artem Healthtech and contributors
// For license information, please see license.txt


// Date: 01-09-2026
// Author: Anjali Patoliya
// Description: This file contains the client-side script for the "Artem Biometric Configuration Setting" doctype. It defines a function to fetch employee punch data based on the selected date range and displays the results in a message dialog.

frappe.ui.form.on("Artem Biometric Configuration Setting", {
	fetch_employee_punch_data(frm) {
		if (!frm.doc.from_date) {
			frappe.msgprint(__("Please select From Date."));
			return;
		}

		if (!frm.doc.to_date) {
			frappe.msgprint(__("Please select To Date."));
			return;
		}

		if (frm.doc.from_date > frm.doc.to_date) {
			frappe.msgprint(__("From Date cannot be greater than To Date."));
			return;
		}

		const fetch_punch_data = () => {
			frappe.call({
				method:
					"artem_biometric.artem_biometric_configration.doctype.artem_biometric_configuration_setting.artem_biometric_configuration_setting.fetch_employee_punch_data",

				args: {
					from_date: frm.doc.from_date,
					to_date: frm.doc.to_date,
				},

				freeze: true,
				freeze_message: __("Fetching biometric punch data..."),

				callback: function (r) {
					if (r.message?.success) {
						frm.reload_doc();

						if (r.message.already_exist) {
							frappe.msgprint(
								__(r.message.message || "Already exist employees punch in employee checkin for select date")
							);
						} else {
							frappe.msgprint(
								__(
									"Created Employee Checkins: {0}<br>Failed to Create Employee Checkins: {1}",
									[
										r.message.processed,
										r.message.failed,
									]
								)
							);
						}
					}
				},
			});
		};

		// Save only if there are unsaved changes
		if (frm.is_dirty()) {
			frm.save().then(() => {
				fetch_punch_data();
			});
		} else {
			fetch_punch_data();
		}
	},
});