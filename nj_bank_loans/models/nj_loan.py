import datetime
import re
from collections import defaultdict

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import float_round, format_amount, format_date

from ..tools import engine

PAY_KINDS = ('principal', 'interest', 'fee')


def _money(env, amount, currency):
    return format_amount(env, amount, currency)


class NjLoan(models.Model):
    _name = 'nj.loan'
    _description = "Банковский кредит"
    _inherit = ['mail.thread', 'mail.activity.mixin']
    _order = 'next_payment_date, name'
    _check_company_auto = True

    name = fields.Char("Код", required=True, tracking=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one('res.company', "Компания", required=True, index=True,
                                 default=lambda self: self.env.company)
    bank_id = fields.Many2one('res.partner', "Банк", required=True, tracking=True, index=True)
    bank_label = fields.Char("Название в Excel")
    contract = fields.Char("Договор", required=True, tracking=True)
    contract_date = fields.Date("Дата договора")
    currency_id = fields.Many2one(
        'res.currency', "Валюта", required=True,
        default=lambda self: self.env.ref('base.USD', raise_if_not_found=False) or self.env.company.currency_id)
    user_id = fields.Many2one('res.users', "Ответственный", default=lambda self: self.env.user, tracking=True)
    state = fields.Selection([('draft', "Черновик"), ('active', "Действует"), ('closed', "Погашен")],
                             "Статус", default='draft', required=True, tracking=True)
    basis = fields.Selection([('360', "360 дней"), ('365', "365 дней")], "База начисления",
                             compute='_compute_rate', store=True)
    accrual_base = fields.Selection(
        [('prev', "Остаток предыдущего дня"), ('same', "Остаток текущего дня")],
        "Проценты дня считаются от", default='prev', required=True,
        help="Как в Excel: формула «Проценты» ссылается на остаток предыдущей строки или той же строки.")
    interest_window = fields.Selection(
        [('start', "С дня прошлой выплаты по день до выплаты"),
         ('end', "Со дня после прошлой выплаты по день выплаты")],
        "Период уплаты %", default='start', required=True)
    prepay_mode = fields.Selection(
        [('term', "Сокращение срока"), ('payment', "Уменьшение платежа"), ('bank', "Новый график банка")],
        "Досрочное погашение", default='term', required=True, tracking=True)
    match_keys = fields.Char(
        "Ключи в выписке", tracking=True,
        help="Через запятую: номер договора или транша так, как он пишется в назначении платежа "
             "(например 2026/11, 0231/02). По ним строка выписки привязывается к кредиту.")
    principal_account_id = fields.Many2one('account.account', "Счёт долга", check_company=True,
                                           default=lambda self: self._default_account('7811'))
    interest_account_id = fields.Many2one('account.account', "Счёт процентов", check_company=True,
                                          default=lambda self: self._default_account('6921'))
    excel_key = fields.Char("Блок Excel", copy=False, index=True)
    note = fields.Html("Примечание")

    line_ids = fields.One2many('nj.loan.line', 'loan_id', "График")
    rate_ids = fields.One2many('nj.loan.rate', 'loan_id', "Ставки")
    payment_ids = fields.One2many('nj.loan.payment', 'loan_id', "Факт из банка")
    penalty_ids = fields.One2many('nj.loan.payment', 'loan_id', "Пени", domain=[('kind', '=', 'penalty')])
    prepayment_ids = fields.One2many('nj.loan.prepayment', 'loan_id', "Досрочные погашения")
    penalty_rate = fields.Float(
        "Пеня, % в день", digits=(16, 4), tracking=True,
        help="Из договора: пеня за день просрочки от просроченной суммы. Нужна для оценки «стоимость дня просрочки».")
    penalty_total = fields.Monetary("Пени всего", compute='_compute_effects')
    gain_total = fields.Monetary("Экономия от досрочных", compute='_compute_effects')
    month_ids = fields.One2many('nj.loan.month', 'loan_id', "По месяцам")
    adjustment_count = fields.Integer(compute='_compute_counts', string="Корректировок")
    payment_count = fields.Integer(compute='_compute_counts', string="Платежей факт")

    amount = fields.Monetary("Сумма кредита", compute='_compute_amounts', store=True)
    date_start = fields.Date("Дата выдачи", compute='_compute_amounts', store=True)
    date_end = fields.Date("Погашение до", compute='_compute_amounts', store=True)
    rate = fields.Float("Ставка, %", compute='_compute_rate', store=True, digits=(16, 2))

    # refreshed daily (depend on today)
    balance = fields.Monetary("Остаток", readonly=True, copy=False)
    next_payment_date = fields.Date("След. платёж", readonly=True, copy=False)
    next_payment_amount = fields.Monetary("Сумма платежа", readonly=True, copy=False)
    days_to_next = fields.Integer("Дней", readonly=True, copy=False)
    urgency = fields.Selection([('overdue', "Просрочено"), ('urgent', "Срочно"), ('soon', "Скоро"),
                                ('later', "В графике"), ('none', "Нет платежей")],
                               "Срок", readonly=True, copy=False, default='none')
    status_date = fields.Date("Пересчитано на", readonly=True, copy=False)
    overdue_amount = fields.Monetary("Просрочено", readonly=True, copy=False)
    overdue_since = fields.Date("Просрочка с", readonly=True, copy=False)
    delay_cost_day = fields.Monetary("Стоимость дня просрочки", readonly=True, copy=False,
                                     help="Просроченная сумма × пеня % в день.")

    _excel_key_uniq = models.Constraint('unique(company_id, excel_key)', "Блок Excel уже загружен в другой кредит.")

    def _default_account(self, code):
        return self.env['account.account'].search(
            [('code', '=', code), ('company_ids', 'in', self.env.company.id)], limit=1)

    @api.depends('line_ids.kind', 'line_ids.amount', 'line_ids.date')
    def _compute_amounts(self):
        for loan in self:
            disb = loan.line_ids.filtered(lambda l: l.kind == 'disb')
            princ = loan.line_ids.filtered(lambda l: l.kind == 'principal' and l.amount)
            loan.amount = sum(disb.mapped('amount'))
            loan.date_start = min(disb.mapped('date')) if disb else False
            loan.date_end = max(princ.mapped('date')) if princ else False

    @api.depends('rate_ids.rate', 'rate_ids.date_from', 'rate_ids.basis')
    def _compute_rate(self):
        for loan in self:
            rates = loan.rate_ids.sorted('date_from')
            loan.rate = rates[-1].rate if rates else 0.0
            loan.basis = rates[-1].basis if rates else '360'

    @api.depends('payment_ids.amount', 'payment_ids.kind', 'prepayment_ids.gain')
    def _compute_effects(self):
        for loan in self:
            loan.penalty_total = sum(loan.payment_ids.filtered(lambda p: p.kind == 'penalty').mapped('amount'))
            loan.gain_total = sum(loan.prepayment_ids.mapped('gain'))

    def _compute_counts(self):
        for loan in self:
            loan.adjustment_count = len(loan.line_ids.filtered(lambda l: l.kind == 'adj'))
            loan.payment_count = len(loan.payment_ids)

    def current_rate(self, day=None):
        self.ensure_one()
        day = day or fields.Date.context_today(self)
        rates = self.rate_ids.filtered(lambda r: r.date_from <= day).sorted('date_from')
        return rates[-1] if rates else self.rate_ids.sorted('date_from')[:1]

    # ------------------------------------------------------------------ engine
    def _engine_events(self, lines=None):
        lines = self.line_ids if lines is None else lines
        return [(l.date, l.kind, l.amount) for l in lines]

    def _engine_rates(self):
        return [(r.date_from, r.rate, int(r.basis)) for r in self.rate_ids]

    def _daily(self, date_from=None, date_to=None, events=None):
        self.ensure_one()
        evs = self._engine_events() if events is None else events
        return engine.daily(evs, self._engine_rates(), date_from, date_to, base=self.accrual_base)

    def _interest_for_payment(self, acc, pay_date, prev_date):
        return engine.window_sum(acc, prev_date, pay_date, self.interest_window)

    # ------------------------------------------------------------------ status
    def _refresh_status(self, today=None):
        today = today or fields.Date.context_today(self)
        overdue = {}
        if self:
            for loan, amount, since, _lines in self._overdue(today, loans=self):
                overdue[loan] = (amount, since)
        for loan in self:
            co = loan.company_id
            lines = loan.line_ids
            bal = sum(l.amount for l in lines if l.kind == 'disb' and l.date <= today) \
                - sum(l.amount for l in lines if l.kind == 'principal' and l.date <= today)
            future = lines.filtered(lambda l: l.kind in PAY_KINDS and l.date >= today and l.amount)
            vals = {'balance': bal, 'status_date': today}
            if future:
                nd = min(future.mapped('date'))
                days = (nd - today).days
                vals.update(next_payment_date=nd, days_to_next=days,
                            next_payment_amount=sum(future.filtered(lambda l: l.date == nd).mapped('amount')),
                            urgency='urgent' if days <= (co.nj_urgent_days or 0)
                            else 'soon' if days <= (co.nj_soon_days or 0) else 'later')
            else:
                vals.update(next_payment_date=False, days_to_next=0, next_payment_amount=0.0, urgency='none')
            late = overdue.get(loan)
            if late:
                amt, since = late
                vals.update(urgency='overdue', overdue_amount=amt, overdue_since=since,
                            delay_cost_day=float_round(amt * (loan.penalty_rate or 0.0) / 100.0,
                                                       precision_rounding=loan.currency_id.rounding))
            else:
                vals.update(overdue_amount=0.0, overdue_since=False, delay_cost_day=0.0)
            if loan.state == 'active' and not future and lines and abs(bal) < 0.005:
                vals['state'] = 'closed'
            elif loan.state == 'closed' and (future or abs(bal) >= 0.005):
                vals['state'] = 'active'
            loan.write(vals)

    def action_activate(self):
        self.write({'state': 'active'})
        self._refresh_status()

    def action_set_draft(self):
        self.write({'state': 'draft'})

    # ------------------------------------------------------------------ actions
    def action_open_daily(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('nj_bank_loans.action_nj_loan_day')
        action['domain'] = [('loan_id', '=', self.id)]
        action['context'] = {'search_default_year_current': 1}
        action['name'] = "Дневной вид: %s" % self.display_name
        return action

    def action_open_planfact(self):
        self.ensure_one()
        action = self.env['ir.actions.act_window']._for_xml_id('nj_bank_loans.action_nj_loan_planfact')
        action['domain'] = [('loan_id', '=', self.id)]
        action['context'] = {}
        return action

    def action_open_adjustments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': "Корректировки начисления",
            'res_model': 'nj.loan.line', 'view_mode': 'list,form',
            'domain': [('loan_id', '=', self.id), ('kind', '=', 'adj')],
            'context': {'default_loan_id': self.id, 'default_kind': 'adj'},
        }

    def action_open_prepayments(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': "Досрочные погашения: %s" % self.name,
                'res_model': 'nj.loan.prepayment', 'view_mode': 'list,pivot',
                'domain': [('loan_id', '=', self.id)], 'context': {'create': False}}

    def action_open_penalties(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': "Пени: %s" % self.name, 'res_model': 'nj.loan.payment',
                'view_mode': 'list,pivot', 'domain': [('loan_id', '=', self.id), ('kind', '=', 'penalty')],
                'context': {'create': False}}

    def action_open_payments(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window', 'name': "Факт из банка",
            'res_model': 'nj.loan.payment', 'view_mode': 'list,form',
            'domain': [('loan_id', '=', self.id)],
        }

    def action_recompute_interest(self):
        """Recalculate future interest payments from the daily accrual (after a rate change)."""
        today = fields.Date.context_today(self)
        for loan in self:
            n = loan._recompute_interest_from(today, old_events=None)
            loan.message_post(body="Пересчитаны будущие выплаты процентов: %s строк." % n)
        self._refresh_status()

    def _recompute_interest_from(self, date_from, old_events=None):
        """Future interest payments (date > date_from) = old amount + change of the accrual
        in its window.  Without old_events every future payment is set to the accrual itself."""
        self.ensure_one()
        lines = self.line_ids.filtered(lambda l: l.kind == 'interest').sorted('date')
        if not lines:
            return 0
        new_acc = engine.accrual_map(self._daily())
        old_acc = engine.accrual_map(engine.daily(old_events, self._engine_rates(), base=self.accrual_base)) \
            if old_events is not None else None
        prev = self.date_start
        changed = 0
        to_unlink = self.env['nj.loan.line']
        for line in lines:
            start = prev or line.date
            prev = line.date
            if line.date <= date_from:
                continue
            new = self._interest_for_payment(new_acc, line.date, start)
            if old_acc is not None:
                old = self._interest_for_payment(old_acc, line.date, start)
                amount = line.amount + (new - old)
            else:
                amount = new
            amount = float_round(amount, precision_rounding=self.currency_id.rounding)
            if amount < self.currency_id.rounding:
                to_unlink |= line
            elif abs(amount - line.amount) >= self.currency_id.rounding:
                line.with_context(nj_no_refresh=True).write({'amount': amount, 'source': 'recalc'})
            else:
                continue
            changed += 1
        to_unlink.with_context(nj_no_refresh=True).unlink()
        return changed

    # ------------------------------------------------------------------ matching
    @api.model
    def _match_text(self, line):
        return ' '.join(filter(None, [line.name, line.move_id.ref, line.move_id.name])).casefold()

    @api.model
    def _loan_keys(self, loan):
        return [k.strip().casefold() for k in re.split(r'[,;\n]', loan.match_keys or '') if k.strip()]

    @api.model
    def _line_amount(self, line, loan):
        if line.currency_id == loan.currency_id:
            return abs(line.amount_currency)
        return line.company_currency_id._convert(abs(line.balance), loan.currency_id, line.company_id, line.date)

    @api.model
    def _nj_place_menu(self):
        """Keep «Кредиты» in Accounting → Accounting right after «Assets & Liabilities»
        (the standard «Loans» lives there in Enterprise)."""
        root = self.env.ref('nj_bank_loans.menu_nj_loan_root', raise_if_not_found=False)
        parent = self.env.ref('account.menu_finance_entries', raise_if_not_found=False)
        assets = self.env.ref('accountant.account_assets_liabilities_menu', raise_if_not_found=False)
        if root and parent:
            vals = {'parent_id': parent.id}
            if assets and assets.parent_id == parent:
                vals['sequence'] = assets.sequence + 1
            root.sudo().write(vals)
        return True

    # ------------------------------------------------------------------ early repayment
    def _future_interest(self, date):
        self.ensure_one()
        return sum(self.line_ids.filtered(lambda l: l.kind in ('interest', 'fee') and l.date > date).mapped('amount'))

    def _apply_prepayment(self, date, amount, mode=None, note=None, source='manual', payment=None):
        """Add an early repayment to the schedule, recalculate the future and record the gain
        (interest of the schedule after the date: before − after)."""
        self.ensure_one()
        loan = self
        cur = loan.currency_id
        mode = mode or loan.prepay_mode
        if amount <= 0:
            raise UserError("Сумма должна быть больше нуля.")
        bal = sum(l.amount for l in loan.line_ids if l.kind == 'disb' and l.date <= date) \
            - sum(l.amount for l in loan.line_ids if l.kind == 'principal' and l.date <= date)
        if amount - bal > cur.rounding:
            raise UserError("Сумма больше остатка долга на дату (%s)." % format_amount(self.env, bal, cur))
        before = loan._future_interest(date)
        old_events = loan._engine_events()
        Line = self.env['nj.loan.line'].with_context(nj_no_refresh=True)
        future = loan.line_ids.filtered(lambda l: l.kind == 'principal' and l.date > date).sorted('date')
        Line.create({'loan_id': loan.id, 'date': date, 'kind': 'principal', 'amount': amount,
                     'source': 'bank' if source == 'auto' else 'manual',
                     'note': "Досрочное погашение" + (": %s" % note if note else "")})
        if mode == 'term':
            rest = amount
            for line in future.sorted('date', reverse=True):
                if rest < cur.rounding:
                    break
                take = min(line.amount, rest)
                rest = float_round(rest - take, precision_rounding=cur.rounding)
                new = float_round(line.amount - take, precision_rounding=cur.rounding)
                if new < cur.rounding:
                    line.with_context(nj_no_refresh=True).unlink()
                else:
                    line.with_context(nj_no_refresh=True).write({'amount': new, 'source': 'recalc'})
        elif mode == 'payment' and future:
            total = sum(future.mapped('amount'))
            factor = max(total - amount, 0.0) / total if total else 0.0
            target = float_round(total - amount, precision_rounding=cur.rounding)
            done = 0.0
            for i, line in enumerate(future):
                new = float_round(line.amount * factor, precision_rounding=cur.rounding)
                if i == len(future) - 1:
                    new = float_round(target - done, precision_rounding=cur.rounding)
                done += new
                if new < cur.rounding:
                    line.with_context(nj_no_refresh=True).unlink()
                else:
                    line.with_context(nj_no_refresh=True).write({'amount': new, 'source': 'recalc'})
        n_int = loan._recompute_interest_from(date, old_events=old_events) if mode != 'bank' else 0
        after = loan._future_interest(date)
        prepay = self.env['nj.loan.prepayment'].create({
            'loan_id': loan.id, 'date': date, 'amount': amount, 'mode': mode, 'source': source,
            'payment_id': payment.id if payment else False,
            'interest_before': before, 'interest_after': after if mode != 'bank' else before,
        })
        loan._refresh_status()
        msg = Markup("Досрочное погашение %s на %s (%s), режим «%s». Пересчитано выплат процентов: %s.") % (
            format_amount(self.env, amount, cur), format_date(self.env, date),
            dict(prepay._fields['source'].selection)[source], dict(prepay._fields['mode'].selection)[mode], n_int)
        if mode != 'bank':
            msg += Markup("<br/>Экономия процентов (расчётная): <b>%s</b>. После получения графика банка загрузите его.") % (
                format_amount(self.env, prepay.gain, cur))
        else:
            msg += Markup("<br/>Ждём новый график банка — экономия будет посчитана после его загрузки.")
        loan.message_post(body=msg)
        return prepay

    def _detect_prepayments(self, today=None):
        """Bank principal payments above the schedule (net of interest booked to the principal account
        by mistake, like K-02 in September 2026) → automatic early repayment."""
        created = self.env['nj.loan.prepayment']
        for loan in self.filtered(lambda l: l.state == 'active'):
            co = loan.company_id
            start = co.nj_fact_start_date
            if not start:
                continue
            cur = loan.currency_id
            window = datetime.timedelta(days=co.nj_prepay_window or 31)
            tol = (co.nj_match_tolerance or 1.0) / 100.0
            facts = loan.payment_ids.filtered(lambda p: p.kind == 'principal' and p.date >= start).sorted('date')
            for pay in facts:
                if loan.prepayment_ids.filtered(lambda r: r.payment_id == pay):
                    continue
                d = pay.date
                plan_p = sum(loan.line_ids.filtered(
                    lambda l: l.kind == 'principal' and start <= l.date <= d + window).mapped('amount'))
                fact_p = sum(loan.payment_ids.filtered(
                    lambda p: p.kind == 'principal' and start <= p.date <= d).mapped('amount'))
                # interest booked to the principal account (K-02, 09.2026) is not a prepayment:
                # count the interest shortfall only for planned interest that WAS paid (close to its date)
                near = datetime.timedelta(days=co.nj_match_days or 10)
                ints = loan.payment_ids.filtered(lambda p: p.kind == 'interest' and p.date >= start)
                shortfall = 0.0
                for pl in loan.line_ids.filtered(
                        lambda l: l.kind in ('interest', 'fee') and start <= l.date <= d + window):
                    paid = ints.filtered(lambda p: abs(p.date - pl.date) <= near)
                    if paid:
                        shortfall += max(pl.amount - sum(paid.mapped('amount')), 0.0)
                excess = fact_p - plan_p - shortfall
                excess = min(float_round(excess, precision_rounding=cur.rounding), pay.amount)
                if excess > max(1.0, pay.amount * tol):
                    created |= loan._apply_prepayment(d, excess, source='auto', payment=pay,
                                                      note=pay.label or "из выписки")
        return created

    # ------------------------------------------------------------------ penalties
    @api.model
    def _penalty_words(self, company):
        return [w.strip().casefold() for w in (company.nj_penalty_keywords or '').split(',') if w.strip()]

    # a bank statement of the day is posted as ONE move with many lines: the text of a
    # neighbour line must not make this line a penalty, so sibling lines are read only
    # for a simple entry (bank line + counterpart, plus exchange lines).
    SIBLING_LIMIT = 4

    @api.model
    def _penalty_scope(self, line):
        """Text and partners that describe THIS cash line, not its whole bank statement."""
        parts = [line.name, line.move_id.ref, line.move_id.name]
        partners = line.partner_id
        if len(line.move_id.line_ids) <= self.SIBLING_LIMIT:
            parts += line.move_id.line_ids.mapped('name')
            partners |= line.move_id.line_ids.partner_id
        return ' '.join(filter(None, parts)).casefold(), partners.commercial_partner_id

    @api.model
    def match_penalties(self):
        """Bank account outflows to a lender with a penalty word in the text → penalty of a loan."""
        loans = self.search([('state', 'in', ('active', 'closed'))])
        Payment = self.env['nj.loan.payment']
        created = 0
        for company in loans.company_id:
            words = self._penalty_words(company)
            co_loans = loans.filtered(lambda l: l.company_id == company)
            if not words or not co_loans:
                continue
            text_dom = []
            for w in words:
                text_dom = (['|'] + text_dom if text_dom else []) + [('name', 'ilike', w)]
            base = [('parent_state', '=', 'posted'), ('company_id', '=', company.id)]
            if company.nj_fact_start_date:
                base.append(('date', '>=', company.nj_fact_start_date))
            # the word may be on the bank line or on its counterpart line: take whole moves
            moves = self.env['account.move.line'].search(base + text_dom).move_id
            banks = co_loans.bank_id.commercial_partner_id
            lines = moves.line_ids.filtered(
                lambda l: l.account_id.account_type == 'asset_cash' and l.credit > 0
                and not l.nj_loan_payment_ids
                and (not l.partner_id or l.partner_id.commercial_partner_id in banks)
                # cheap pre-filter: on a many-line statement only the line itself counts
                and (len(l.move_id.line_ids) <= self.SIBLING_LIMIT
                     or any(w in (l.name or '').casefold() for w in words)))
            for line in lines.sorted(lambda l: (l.date, l.id)):
                text, partners = self._penalty_scope(line)
                if not any(w in text for w in words):
                    continue
                cands = co_loans.filtered(lambda l: l.bank_id.commercial_partner_id in partners)
                if not cands:
                    cands = co_loans.filtered(lambda l: any(k in text for k in self._loan_keys(l)))
                if not cands:
                    continue
                by_key = cands.filtered(lambda l: any(k in text for k in self._loan_keys(l)))
                if len(by_key) == 1:
                    loan, method = by_key, 'key'
                elif len(cands) == 1:
                    loan, method = cands, 'single'
                else:
                    late = self._overdue(line.date + datetime.timedelta(days=1), loans=cands)
                    loan = max(late, key=lambda t: t[2])[0] if late else cands.sorted('balance')[-1]
                    method = 'overdue'
                Payment.create({'loan_id': loan.id, 'move_line_id': line.id, 'kind': 'penalty',
                                'amount': self._line_amount(line, loan), 'match_method': method})
                loan.message_post(body=Markup("Списана пеня %s (%s): %s") % (
                    format_amount(self.env, self._line_amount(line, loan), loan.currency_id),
                    format_date(self.env, line.date), line.name or line.move_id.ref or ''))
                created += 1
        return created

    def _unmatched_domain(self, loans=None):
        loans = loans if loans is not None else self.search([('state', 'in', ('active', 'closed'))])
        accounts = loans.principal_account_id | loans.interest_account_id
        banks = loans.bank_id.commercial_partner_id
        domain = [('parent_state', '=', 'posted'), ('account_id', 'in', accounts.ids),
                  ('partner_id', 'child_of', banks.ids), ('debit', '>', 0),
                  ('nj_loan_payment_ids', '=', False), ('company_id', 'in', loans.company_id.ids)]
        starts = [d for d in loans.company_id.mapped('nj_fact_start_date') if d]
        if starts:
            domain.append(('date', '>=', min(starts)))
        return domain

    @api.model
    def match_statement_lines(self, lines=None):
        """Link posted bank statement lines (debit of 7811 / 6921) to loans.
        1) a key of the loan in the payment text; 2) the only loan of the bank;
        3) a planned payment of the same kind within ±N days and ±tolerance %."""
        loans = self.search([('state', 'in', ('active', 'closed'))])
        if not loans:
            return 0
        if lines is None:
            lines = self.env['account.move.line'].search(self._unmatched_domain(loans), order='date, id')
        Payment = self.env['nj.loan.payment']
        created = 0
        for line in lines:
            if line.nj_loan_payment_ids:
                continue
            company = line.company_id
            bank = line.partner_id.commercial_partner_id
            cands = loans.filtered(lambda l: l.company_id == company and l.bank_id.commercial_partner_id == bank
                                   and line.account_id in (l.principal_account_id | l.interest_account_id))
            if not cands:
                continue
            kind_of = lambda l: 'principal' if line.account_id == l.principal_account_id else 'interest'
            text = self._match_text(line)
            by_key = cands.filtered(lambda l: any(k in text for k in self._loan_keys(l)))
            loan, method = False, False
            if len(by_key) == 1:
                loan, method = by_key, 'key'
            elif not by_key and len(cands) == 1:
                loan, method = cands, 'single'
            elif not by_key:
                window = datetime.timedelta(days=company.nj_match_days or 10)
                tol = (company.nj_match_tolerance or 1.0) / 100.0
                hits = []
                for cand in cands:
                    kind = kind_of(cand)
                    kinds = ('principal',) if kind == 'principal' else ('interest', 'fee')
                    amt = self._line_amount(line, cand)
                    plans = cand.line_ids.filtered(
                        lambda p: p.kind in kinds and abs((p.date - line.date).days) <= window.days and p.amount)
                    if any(abs(p.amount - amt) <= max(p.amount * tol, cand.currency_id.rounding) for p in plans):
                        hits.append(cand)
                if len(hits) == 1:
                    loan, method = hits[0], 'amount'
            if not loan:
                continue
            Payment.create({
                'loan_id': loan.id, 'move_line_id': line.id, 'kind': kind_of(loan),
                'amount': self._line_amount(line, loan), 'match_method': method,
            })
            created += 1
        created += self.match_penalties()
        loans._detect_prepayments()
        return created

    def action_match_now(self):
        n = self.match_statement_lines()
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'title': "Сопоставление выписок", 'type': 'info', 'sticky': False,
                           'message': "Привязано строк выписки: %s" % n,
                           'next': {'type': 'ir.actions.act_window_close'}}}

    @api.model
    def action_open_unmatched(self):
        return {
            'type': 'ir.actions.act_window', 'name': "Не сопоставлено",
            'res_model': 'account.move.line', 'view_mode': 'list',
            'views': [(self.env.ref('nj_bank_loans.view_move_line_nj_unmatched').id, 'list')],
            'domain': self._unmatched_domain(), 'context': {'create': False},
        }

    # ------------------------------------------------------------------ reminders
    @api.model
    def _upcoming(self, today, days_ahead):
        """Planned payments grouped by (company, bank, date) for active loans."""
        lines = self.env['nj.loan.line'].search([
            ('loan_id.state', '=', 'active'), ('kind', 'in', PAY_KINDS), ('amount', '!=', 0),
            ('date', '>=', today), ('date', '<=', today + datetime.timedelta(days=days_ahead))], order='date')
        groups = defaultdict(lambda: self.env['nj.loan.line'])
        for l in lines:
            groups[(l.loan_id.company_id, l.loan_id.bank_id, l.date, l.loan_id.currency_id)] |= l
        return groups

    @api.model
    def _overdue(self, today, loans=None):
        """What the banks are still owed: [(loan, amount, since, lines)].

        Rules (see history D24, D32):
        * the plan from «statements loaded from» up to the last loaded statement must be covered
          by bank payments up to that date + the matching window (early payments count);
        * principal and interest are NETTED: 1C books part of the interest to the loan account
          (case K-02), the split differs but the money reached the bank — that is not a debt;
        * reported amount is the REMAINDER, not the whole planned line;
        * a shortfall below 1 % of the largest scheduled payment is exchange-rate rounding,
          not a debt — it stays visible in plan-fact and is not raised as overdue.
        """
        res = []
        for company in self.env['res.company'].search([('nj_fact_start_date', '!=', False)]):
            start = company.nj_fact_start_date
            window = datetime.timedelta(days=company.nj_match_days or 10)
            extra = [('loan_id', 'in', loans.ids)] if loans is not None else []
            # statements loaded up to: the last posted bank / cash movement of the company
            last = self.env['account.move.line'].search(
                [('company_id', '=', company.id), ('parent_state', '=', 'posted'),
                 ('account_id.account_type', '=', 'asset_cash')], order='date desc', limit=1).date
            if not last or last < start:
                continue
            limit = min(today - datetime.timedelta(days=1), last)
            lines = self.env['nj.loan.line'].search(extra + [
                ('loan_id.company_id', '=', company.id), ('loan_id.state', 'in', ('active', 'closed')),
                ('kind', 'in', PAY_KINDS), ('amount', '>', 0),
                ('date', '>=', start), ('date', '<', limit)])
            tol_pct = (company.nj_match_tolerance or 1.0) / 100.0
            for loan in lines.loan_id:
                due = lines.filtered(lambda l: l.loan_id == loan).sorted('date')
                plan = loan.line_ids.filtered(lambda x: x.kind in PAY_KINDS and x.date >= start)
                paid = loan.payment_ids.filtered(lambda p: p.kind in PAY_KINDS and p.date >= start)
                tol = max(loan.currency_id.rounding, 1.0, max(due.mapped('amount')) * tol_pct)

                def short(day, plan=plan, paid=paid, window=window):
                    return sum(x.amount for x in plan if x.date <= day) \
                        - sum(p.amount for p in paid if p.date <= day + window)

                amount = short(due[-1].date)
                if amount <= tol:
                    continue
                since = next(d.date for d in due if short(d.date) > tol)
                res.append((loan, float_round(amount, precision_rounding=loan.currency_id.rounding),
                            since, due.filtered(lambda l: l.date >= since)))
        return res

    @api.model
    def _cron_daily(self):
        today = fields.Date.context_today(self)
        self.search([('state', 'in', ('active', 'closed'))])._refresh_status(today)
        self.match_statement_lines()
        self._create_reminder_activities(today)

    @api.model
    def _create_reminder_activities(self, today):
        todo = self.env.ref('mail.mail_activity_data_todo')
        Activity = self.env['mail.activity']
        model_id = self.env['ir.model']._get_id('nj.loan')
        created = 0
        companies = self.env['res.company'].search([])
        max_days = 0
        for co in companies:
            days = [int(x) for x in re.findall(r'\d+', co.nj_remind_days or '')]
            max_days = max([max_days] + days)
        if max_days:
            for (company, bank, day, currency), lines in self._upcoming(today, max_days).items():
                days = [int(x) for x in re.findall(r'\d+', company.nj_remind_days or '')]
                if not days or (day - today).days > max(days):
                    continue
                for loan in lines.loan_id:
                    amount = sum(lines.filtered(lambda l: l.loan_id == loan).mapped('amount'))
                    summary = "Платёж %s: %s" % (format_date(self.env, day), _money(self.env, amount, currency))
                    if Activity.search_count([('res_model_id', '=', model_id), ('res_id', '=', loan.id),
                                              ('summary', '=', summary)]):
                        continue
                    loan.activity_schedule(
                        activity_type_id=todo.id, date_deadline=day, summary=summary,
                        note="%s · %s · подготовить деньги на счёте" % (bank.display_name, loan.contract),
                        user_id=(loan.user_id or self.env.user).id)
                    created += 1
        for loan, amount, since, _lines in self._overdue(today):
            summary = "Нет оплаты %s: %s" % (format_date(self.env, since),
                                             _money(self.env, amount, loan.currency_id))
            note = "В выписке банка не найдена оплата планового платежа."
            if loan.delay_cost_day:
                note += " Стоимость дня просрочки ≈ %s." % _money(self.env, loan.delay_cost_day, loan.currency_id)
            for user in (loan.company_id._nj_recipients('overdue') or loan.user_id or self.env.user):
                if Activity.search_count([('res_model_id', '=', model_id), ('res_id', '=', loan.id),
                                          ('summary', '=', summary), ('user_id', '=', user.id)]):
                    continue
                loan.activity_schedule(activity_type_id=todo.id, date_deadline=today, summary=summary,
                                       note=note, user_id=user.id)
                created += 1
        return created

    # events older than this are not shown to a user who has never seen a pop-up
    NOTICE_LOOKBACK = 30

    @api.model
    def nj_popup_reminders(self):
        """Sticky notifications, each shown ONCE per user (R23).

        red    — overdue, penalty debited by the bank, payment due within «Срочно» days (3)
        yellow — payment due within «Скоро» days (7)
        green  — early repayment found

        A payment is announced twice at most: yellow when it enters the «soon» window and red
        when it enters the «urgent» one — these are different events, not a repeat.
        Each event goes only to the users ticked for it in the settings (empty → the CFO).
        """
        user = self.env.user
        if not user.has_group('nj_bank_loans.group_nj_loan_user'):
            return []
        today = fields.Date.context_today(self)
        since = today - datetime.timedelta(days=self.NOTICE_LOOKBACK)
        Notice = self.env['nj.loan.notice'].sudo()
        seen = set(Notice.search([('user_id', '=', user.id)]).mapped('key'))
        fresh = []

        def once(key):
            if key in seen or key in fresh:
                return False
            fresh.append(key)
            return True

        res = []
        companies = user.company_ids.filtered(lambda c: user in c._nj_recipients('due'))
        if companies:
            soon = max(companies.mapped('nj_soon_days') or [7])
            groups = self.sudo()._upcoming(today, soon)
            for (co, bank, day, currency), lines in sorted(groups.items(), key=lambda kv: kv[0][2]):
                days = (day - today).days
                if co not in companies or days > (co.nj_soon_days or 7):
                    continue
                level = 'danger' if days <= (co.nj_urgent_days or 0) else 'warning'
                names = ", ".join(sorted(lines.loan_id.mapped('name')))
                if not once("due:%s:%s:%s" % (day, names, level)):
                    continue
                when = "сегодня" if days == 0 else "через %s дн" % days
                res.append({
                    'type': level,
                    'title': "Платёж по кредиту: %s" % when,
                    'message': "%s · %s · %s (%s)" % (format_date(self.env, day), bank.display_name,
                                                     _money(self.env, sum(lines.mapped('amount')), currency),
                                                     names),
                })
        companies = user.company_ids.filtered(lambda c: user in c._nj_recipients('overdue'))
        parts = []
        for loan, amount, start, _lines in self.sudo()._overdue(today):
            if loan.company_id not in companies or not once("late:%s:%s" % (loan.id, start)):
                continue
            txt = "%s с %s: %s" % (loan.name, format_date(self.env, start),
                                   _money(self.env, amount, loan.currency_id))
            if loan.delay_cost_day:
                txt += " (≈ %s в день)" % _money(self.env, loan.delay_cost_day, loan.currency_id)
            parts.append(txt)
        if parts:
            res.append({'type': 'danger', 'title': "Просрочка: банк не получил деньги",
                        'message': "; ".join(parts[:5])})
        companies = user.company_ids.filtered(lambda c: user in c._nj_recipients('penalty'))
        pens = self.env['nj.loan.payment'].sudo().search([
            ('kind', '=', 'penalty'), ('company_id', 'in', companies.ids), ('date', '>=', since)])
        pens = pens.filtered(lambda p: once("pen:%s" % p.id))
        if pens:
            res.append({'type': 'danger', 'title': "Банк списал пеню: %s" % _money(
                            self.env, sum(pens.mapped('amount')), pens[:1].currency_id),
                        'message': "; ".join("%s %s %s" % (p.loan_id.name, format_date(self.env, p.date),
                                                           _money(self.env, p.amount, p.currency_id))
                                             for p in pens[:5])})
        companies = user.company_ids.filtered(lambda c: user in c._nj_recipients('prepay'))
        pre = self.env['nj.loan.prepayment'].sudo().search([
            ('company_id', 'in', companies.ids), ('date', '>=', since)])
        pre = pre.filtered(lambda r: once("pre:%s" % r.id))
        if pre:
            res.append({'type': 'success', 'title': "Досрочное погашение: экономия %s" % _money(
                            self.env, sum(pre.mapped('gain')), pre[:1].currency_id),
                        'message': "; ".join("%s %s досрочно %s" % (r.loan_id.name, format_date(self.env, r.date),
                                                                    _money(self.env, r.amount, r.currency_id))
                                             for r in pre[:5])})
        if fresh:
            Notice.create([{'user_id': user.id, 'key': k} for k in fresh])
        user.sudo().nj_reminder_date = today
        return res
