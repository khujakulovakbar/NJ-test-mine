import { registry } from "@web/core/registry";

/**
 * Once a day per user: sticky notifications about the nearest loan payments
 * (data from nj.loan.nj_popup_reminders; empty for users without access).
 */
export const njLoanReminderService = {
    dependencies: ["orm", "notification"],
    start(env, { orm, notification }) {
        setTimeout(async () => {
            let items = [];
            try {
                items = await orm.silent.call("nj.loan", "nj_popup_reminders", []);
            } catch {
                return;
            }
            for (const item of items || []) {
                notification.add(item.message, { title: item.title, type: item.type, sticky: true });
            }
        }, 2000);
    },
};

registry.category("services").add("nj_loan_reminder", njLoanReminderService);
