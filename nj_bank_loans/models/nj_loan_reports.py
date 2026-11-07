from odoo import api, fields, models, tools

KIND_PF = [('principal', "Долг"), ('interest', "Проценты")]


class NjSqlReportMixin(models.AbstractModel):
    """The report views use window functions over the whole schedule; PostgreSQL JIT compilation
    costs ~0.7 s per query on them while the query itself takes ~0.1 s, so JIT is switched off
    for the current transaction before reading."""
    _name = 'nj.sql.report.mixin'
    _description = "Кредиты: SQL-отчёт"

    def _nj_no_jit(self):
        self.env.cr.execute("SET LOCAL jit = off")

    @api.model
    def _read_group(self, *args, **kwargs):
        self._nj_no_jit()
        return super()._read_group(*args, **kwargs)

    def search_fetch(self, *args, **kwargs):
        self._nj_no_jit()
        return super().search_fetch(*args, **kwargs)


class NjLoanDay(models.Model):
    """Daily schedule exactly like the Excel: one row per loan and calendar day."""
    _name = 'nj.loan.day'
    _inherit = ['nj.sql.report.mixin']
    _description = "Кредит: дневной вид"
    _auto = False
    _order = 'loan_id, date'

    loan_id = fields.Many2one('nj.loan', "Кредит", readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    bank_id = fields.Many2one('res.partner', "Банк", readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    date = fields.Date("Дата", readonly=True)
    weekday = fields.Char("ДН", readonly=True)
    interest = fields.Monetary("Проценты", readonly=True)
    disb = fields.Monetary("Получен займ", readonly=True)
    interest_paid = fields.Monetary("Погашен %", readonly=True)
    fee = fields.Monetary("БГ", readonly=True, help="Комиссия банковской гарантии — отдельно от процентов, как в Excel")
    principal = fields.Monetary("Погашен осн. долг", readonly=True)
    balance = fields.Monetary("Остаток", readonly=True, aggregator=None)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW nj_loan_day AS (
            WITH ev AS (
                SELECT loan_id, date,
                       SUM(CASE WHEN kind = 'disb' THEN amount ELSE 0 END) AS disb,
                       SUM(CASE WHEN kind = 'principal' THEN amount ELSE 0 END) AS principal,
                       SUM(CASE WHEN kind = 'interest' THEN amount ELSE 0 END) AS interest_paid,
                       SUM(CASE WHEN kind = 'fee' THEN amount ELSE 0 END) AS fee,
                       SUM(CASE WHEN kind = 'adj' THEN amount ELSE 0 END) AS adj
                  FROM nj_loan_line GROUP BY loan_id, date
            ), rng AS (
                SELECT loan_id, MIN(date) AS d0, MAX(date) AS d1 FROM nj_loan_line GROUP BY loan_id
            ), days AS (
                SELECT r.loan_id, gs::date AS date
                  FROM rng r, generate_series(r.d0, r.d1, interval '1 day') gs
            ), b AS (
                SELECT d.loan_id, d.date,
                       COALESCE(e.disb, 0) AS disb, COALESCE(e.principal, 0) AS principal,
                       COALESCE(e.interest_paid, 0) AS interest_paid, COALESCE(e.fee, 0) AS fee,
                       COALESCE(e.adj, 0) AS adj,
                       SUM(COALESCE(e.disb, 0) - COALESCE(e.principal, 0))
                           OVER (PARTITION BY d.loan_id ORDER BY d.date) AS balance
                  FROM days d LEFT JOIN ev e ON e.loan_id = d.loan_id AND e.date = d.date
            ), w AS (
                SELECT b.*, COALESCE(LAG(b.balance) OVER (PARTITION BY b.loan_id ORDER BY b.date), 0) AS prev_balance
                  FROM b
            )
            SELECT (w.loan_id::bigint * 100000 + (w.date - DATE '2000-01-01')) AS id,
                   w.loan_id, l.company_id, l.bank_id, l.currency_id, w.date,
                   (ARRAY['ПН','ВТ','СР','ЧТ','ПТ','СБ','ВС'])[EXTRACT(ISODOW FROM w.date)::int] AS weekday,
                   (CASE WHEN l.accrual_base = 'same' THEN w.balance ELSE w.prev_balance END)
                       * COALESCE(r.rate, 0) / 100.0
                       / (CASE WHEN r.basis = '365' THEN 365 ELSE 360 END) + w.adj AS interest,
                   w.disb, w.interest_paid, w.fee, w.principal, w.balance
              FROM w
              JOIN nj_loan l ON l.id = w.loan_id
              LEFT JOIN LATERAL (
                    SELECT rt.rate, rt.basis FROM nj_loan_rate rt
                     WHERE rt.loan_id = w.loan_id AND rt.date_from <= w.date
                     ORDER BY rt.date_from DESC LIMIT 1) r ON TRUE
            )""")


class NjLoanMonth(models.Model):
    _name = 'nj.loan.month'
    _inherit = ['nj.sql.report.mixin']
    _description = "Кредит: по месяцам"
    _auto = False
    _order = 'month, loan_id'

    loan_id = fields.Many2one('nj.loan', "Кредит", readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    bank_id = fields.Many2one('res.partner', "Банк", readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    month = fields.Date("Месяц", readonly=True)
    interest_accrued = fields.Monetary("Начислено %", readonly=True)
    disb = fields.Monetary("Получен займ", readonly=True)
    interest_paid = fields.Monetary("Погашен %", readonly=True)
    fee = fields.Monetary("БГ", readonly=True, help="Комиссия банковской гарантии — отдельно от процентов, как в Excel")
    principal = fields.Monetary("Погашен осн. долг", readonly=True)
    payment = fields.Monetary("Платёж (долг + % + БГ)", readonly=True)
    balance_end = fields.Monetary("Остаток на конец месяца", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW nj_loan_month AS (
            SELECT (d.loan_id::bigint * 100000 + EXTRACT(YEAR FROM date_trunc('month', d.date))::int * 12
                        + EXTRACT(MONTH FROM date_trunc('month', d.date))::int) AS id,
                   d.loan_id, d.company_id, d.bank_id, d.currency_id,
                   date_trunc('month', d.date)::date AS month,
                   SUM(d.interest) AS interest_accrued, SUM(d.disb) AS disb,
                   SUM(d.interest_paid) AS interest_paid, SUM(d.fee) AS fee, SUM(d.principal) AS principal,
                   SUM(d.interest_paid + d.fee + d.principal) AS payment,
                   (ARRAY_AGG(d.balance ORDER BY d.date DESC))[1] AS balance_end
              FROM nj_loan_day d
             GROUP BY d.loan_id, d.company_id, d.bank_id, d.currency_id, date_trunc('month', d.date)
            )""")


class NjLoanPlanFact(models.Model):
    _name = 'nj.loan.planfact'
    _inherit = ['nj.sql.report.mixin']
    _description = "Кредит: план-факт"
    _auto = False
    _order = 'month desc, bank_id, loan_id, kind'

    loan_id = fields.Many2one('nj.loan', "Кредит", readonly=True)
    company_id = fields.Many2one('res.company', readonly=True)
    bank_id = fields.Many2one('res.partner', "Банк", readonly=True)
    currency_id = fields.Many2one('res.currency', readonly=True)
    month = fields.Date("Месяц", readonly=True)
    kind = fields.Selection(KIND_PF, "Вид", readonly=True)
    plan_date = fields.Date("План: дата", readonly=True)
    fact_date = fields.Date("Факт: дата", readonly=True)
    plan_amount = fields.Monetary("План", readonly=True)
    fact_amount = fields.Monetary("Факт", readonly=True)
    diff = fields.Monetary("Разница (факт − план)", readonly=True)
    fact_loaded = fields.Boolean("Выписка загружена", readonly=True)
    has_diff = fields.Boolean("Есть разница", readonly=True)
    status = fields.Selection([('ok', "Оплачено"), ('diff', "Разница"), ('unpaid', "Нет оплаты"),
                               ('wait', "Ожидается"), ('nodata', "Нет выписки"), ('future', "План")],
                              "Статус", readonly=True)
    diff_type = fields.Selection([('timing', "Сроки"), ('amount', "Сумма"), ('account', "Счёт (долг ↔ %)"),
                                  ('missing', "Нет оплаты")], "Тип разницы", readonly=True)

    def init(self):
        tools.drop_view_if_exists(self.env.cr, self._table)
        self.env.cr.execute("""
            CREATE OR REPLACE VIEW nj_loan_planfact AS (
            WITH plan AS (
                SELECT loan_id, date_trunc('month', date)::date AS month,
                       CASE WHEN kind = 'principal' THEN 'principal' ELSE 'interest' END AS kind,
                       SUM(amount) AS amount, MIN(date) AS pdate
                  FROM nj_loan_line WHERE kind IN ('principal', 'interest', 'fee')
                 GROUP BY 1, 2, 3
            ), fact AS (
                SELECT loan_id, date_trunc('month', date)::date AS month, kind,
                       SUM(amount) AS amount, MIN(date) AS fdate
                  FROM nj_loan_payment WHERE kind IN ('principal', 'interest') GROUP BY 1, 2, 3
            ), j AS (
                SELECT COALESCE(p.loan_id, f.loan_id) AS loan_id, COALESCE(p.month, f.month) AS month,
                       COALESCE(p.kind, f.kind) AS kind,
                       COALESCE(p.amount, 0) AS plan_amount, COALESCE(f.amount, 0) AS fact_amount,
                       p.pdate AS plan_date, f.fdate AS fact_date
                  FROM plan p FULL JOIN fact f
                    ON f.loan_id = p.loan_id AND f.month = p.month AND f.kind = p.kind
            ), k AS (
                SELECT j.*, l.company_id, l.bank_id, l.currency_id,
                       j.fact_amount - j.plan_amount AS diff,
                       (c.nj_fact_start_date IS NOT NULL
                        AND j.month >= date_trunc('month', c.nj_fact_start_date)::date
                        AND j.month <= date_trunc('month', CURRENT_DATE)::date) AS fact_loaded
                  FROM j JOIN nj_loan l ON l.id = j.loan_id
                  JOIN res_company c ON c.id = l.company_id
            )
            SELECT (k.loan_id::bigint * 1000000 + (EXTRACT(YEAR FROM k.month)::int * 12
                        + EXTRACT(MONTH FROM k.month)::int) * 10
                        + CASE WHEN k.kind = 'principal' THEN 1 ELSE 2 END) AS id,
                   k.loan_id, k.company_id, k.bank_id, k.currency_id, k.month, k.kind,
                   k.plan_date, k.fact_date, k.plan_amount, k.fact_amount,
                   CASE WHEN k.fact_loaded THEN k.diff ELSE 0 END AS diff, k.fact_loaded,
                   (k.fact_loaded AND ABS(k.diff) >= 0.01) AS has_diff,
                   CASE
                       WHEN NOT k.fact_loaded THEN
                            CASE WHEN k.month > date_trunc('month', CURRENT_DATE)::date THEN 'future' ELSE 'nodata' END
                       WHEN ABS(k.diff) < 0.01 THEN 'ok'
                       WHEN k.fact_amount = 0 AND k.month = date_trunc('month', CURRENT_DATE)::date THEN 'wait'
                       WHEN k.fact_amount = 0 THEN 'unpaid'
                       ELSE 'diff'
                   END AS status,
                   CASE
                       WHEN NOT k.fact_loaded THEN NULL
                       WHEN ABS(k.diff) < 0.01 THEN
                            CASE WHEN k.fact_date IS NOT NULL AND k.plan_date IS NOT NULL
                                      AND ABS(k.fact_date - k.plan_date) > 3 THEN 'timing' END
                       WHEN ABS(SUM(k.diff) OVER (PARTITION BY k.loan_id, k.month)) < 0.01 THEN 'account'
                       WHEN k.fact_amount = 0 THEN 'missing'
                       ELSE 'amount'
                   END AS diff_type
              FROM k
            )""")
