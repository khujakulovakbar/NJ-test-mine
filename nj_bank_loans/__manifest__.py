{
    'name': 'NJ: Банковские кредиты',
    'version': '19.0.1.2.3',
    'category': 'Accounting/Accounting',
    'summary': 'Реестр кредитов, дневной график как в Excel, план-факт по выпискам банка, напоминания о платежах',
    'description': """
Кредиты NJ
==========
* Реестр кредитов: банк, договор, ставки (история), база 360/365, режим досрочного погашения.
* Загрузка главного Excel «Банковские кредиты NJ действующие.xlsx» (график, ставки, корректировки).
* Дневной вид с колонками как в Excel: Проценты, Получен займ, Погашен %, Погашен осн. долг, Остаток.
* Факт: строки выписок банка (счета 7811 / 6921) сопоставляются с кредитами.
* План-факт с типами разниц, сводка по банкам, остаток по банкам, платёжный календарь.
* Досрочное погашение с пересчётом графика, загрузка графика банка, создание графика нового кредита.
* Напоминания: активности, всплывающее окно при входе, контроль просрочки.
""",
    'author': 'NATURAL JUICE',
    'license': 'LGPL-3',
    'depends': ['account', 'mail', 'account_loans'],
    'external_dependencies': {'python': ['openpyxl']},
    'data': [
        'security/nj_loan_security.xml',
        'security/ir.model.access.csv',
        'data/nj_loan_cron.xml',
        'wizard/nj_loan_wizard_views.xml',
        'views/nj_loan_report_views.xml',
        'views/nj_loan_views.xml',
        'views/nj_loan_dashboard_views.xml',
        'views/res_config_settings_views.xml',
        'views/nj_loan_menus.xml',
    ],
    'assets': {
        'web.assets_backend': [
            'nj_bank_loans/static/src/js/reminder_service.js',
        ],
    },
    'application': False,
    'installable': True,
}
