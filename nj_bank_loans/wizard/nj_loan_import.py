import base64
import datetime
import json
import re

from markupsafe import Markup

from odoo import api, fields, models
from odoo.exceptions import UserError
from odoo.tools import format_amount

from ..tools import engine
from ..tools.excel_parser import ExcelLoanError, parse_loans_xlsx

TRANSLIT = {'хамкор': 'hamkor', 'траст': 'trast', 'офб': 'ofb', 'асака': 'asaka', 'капитал': 'kapital',
            'ипотека': 'ipoteka', 'агро': 'agro', 'миллий': 'milliy', 'узпромстрой': 'uzpromstroy'}


def _d(s):
    return datetime.date.fromisoformat(s)


class NjLoanImportWizard(models.TransientModel):
    _name = 'nj.loan.import.wizard'
    _description = "Загрузка кредитов из Excel"

    state = fields.Selection([('upload', "Файл"), ('preview', "Проверка"), ('done', "Готово")], default='upload')
    file = fields.Binary("Файл Excel")
    filename = fields.Char("Имя файла")
    data_json = fields.Text()
    first_date = fields.Date("Первая дата", readonly=True)
    last_date = fields.Date("Последняя дата", readonly=True)
    line_ids = fields.One2many('nj.loan.import.wizard.line', 'wizard_id', "Кредиты в файле")
    result = fields.Html("Результат", readonly=True, sanitize=False)

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
                'view_mode': 'form', 'target': 'new', 'name': "Загрузка кредитов из Excel",
                'context': {'dialog_size': 'extra-large'}}

    @api.model
    def _suggest_partner(self, label):
        words = re.findall(r'[A-Za-zА-Яа-яЁё]+', label or '')
        Partner = self.env['res.partner']
        # 1) banks that already have postings on the loan accounts (7811 / 6921) win:
        #    a plain name search picks e.g. "AS Hamkor-Savdo" instead of the bank itself.
        Loan = self.env['nj.loan']
        accounts = Loan._default_account('7811') | Loan._default_account('6921')
        known = Partner
        if accounts:
            groups = self.env['account.move.line']._read_group(
                [('account_id', 'in', accounts.ids), ('partner_id', '!=', False),
                 ('company_id', '=', self.env.company.id)], ['partner_id'])
            known = Partner.browse([g[0].id for g in groups]).commercial_partner_id
        for word in words[:2]:
            cands = [word]
            low = word.lower()
            for ru, lat in TRANSLIT.items():
                if low.startswith(ru):
                    cands.append(lat)
            for c in cands:
                if len(c) >= 3:
                    hit = known.filtered(lambda p, c=c: c.lower() in (p.name or '').lower())
                    if hit:
                        return hit[:1]
        # 2) fallback: plain name search
        for word in words[:2]:
            cands = [word]
            low = word.lower()
            for ru, lat in TRANSLIT.items():
                if low.startswith(ru):
                    cands.append(lat)
            for c in cands:
                if len(c) < 3:
                    continue
                p = Partner.search([('name', 'ilike', c), ('is_company', '=', True)], limit=1) or \
                    Partner.search([('name', 'ilike', c)], limit=1)
                if p:
                    return p.commercial_partner_id
        return Partner

    def action_parse(self):
        self.ensure_one()
        if not self.file:
            raise UserError("Выберите файл Excel.")
        try:
            res = parse_loans_xlsx(base64.b64decode(self.file))
        except ExcelLoanError as e:
            raise UserError(str(e)) from e
        Loan = self.env['nj.loan']
        blocks, lines = [], [(5, 0, 0)]
        for b in res['blocks']:
            blocks.append({
                'index': b['index'], 'label': b['label'], 'contract': b['contract'],
                'excel_key': b['excel_key'], 'keys': b['keys'], 'base': b['base'], 'window': b['window'],
                'contract_date': b['contract_date'] and b['contract_date'].isoformat(),
                'events': [(d.isoformat(), k, a) for d, k, a in b['events']],
                'rates': [(d.isoformat(), r, bs) for d, r, bs in b['rates']],
                'notes': {d.isoformat(): n for d, _x, n in b['adjustments']},
                'month_end': {d.isoformat(): v for d, v in b['month_end'].items()},
            })
            loan = Loan.search([('excel_key', '=', b['excel_key']), ('company_id', '=', self.env.company.id)], limit=1)
            partner = loan.bank_id or self._suggest_partner(b['label'])
            disb = sum(a for _d0, k, a in b['events'] if k == 'disb')
            rate_txt = ", ".join("%s%% с %s" % (('%.4f' % r).rstrip('0').rstrip('.'), d.strftime('%d.%m.%Y'))
                                 for d, r, _bs in b['rates'][-3:])
            warn = False
            if loan and (loan.prepayment_ids or loan.line_ids.filtered(lambda l: l.source in ('recalc', 'bank'))):
                warn = "В Odoo есть досрочное погашение / график банка — Odoo главный, загрузка перезапишет"
            lines.append((0, 0, {
                'do_import': not warn, 'warning': warn,
                'index': b['index'], 'label': b['label'], 'contract': b['contract'],
                'name': loan.name or 'K-%02d' % b['index'], 'loan_id': loan.id, 'partner_id': partner.id,
                'n_events': len([e for e in b['events'] if e[1] != 'adj']),
                'n_adj': len(b['adjustments']), 'disb_total': disb, 'last_balance': b['last_balance'],
                'basis': str(b['rates'][-1][2]) if b['rates'] else '360', 'rate_text': rate_txt,
                'accrual_base': b['base'], 'interest_window': b['window'],
            }))
        self.write({'state': 'preview', 'data_json': json.dumps(blocks), 'line_ids': lines,
                    'first_date': res['first_date'], 'last_date': res['last_date']})
        return self._reopen()

    def action_import(self):
        self.ensure_one()
        blocks = {b['index']: b for b in json.loads(self.data_json or '[]')}
        missing = self.line_ids.filtered(lambda l: l.do_import and not l.partner_id)
        if missing:
            raise UserError("Укажите банк (контрагента) для: %s" % ", ".join(missing.mapped('label')))
        usd = self.env.ref('base.USD', raise_if_not_found=False) or self.env.company.currency_id
        Loan = self.env['nj.loan'].with_context(tracking_disable=True)
        rows = []
        for wl in self.line_ids.filtered('do_import').sorted('index'):
            b = blocks[wl.index]
            vals = {
                'name': wl.name, 'bank_id': wl.partner_id.id, 'bank_label': b['label'],
                'contract': b['contract'] or b['label'], 'excel_key': b['excel_key'],
                'contract_date': b['contract_date'] and _d(b['contract_date']),
                'accrual_base': b['base'], 'interest_window': b['window'], 'state': 'active',
            }
            loan = wl.loan_id
            if loan:
                old = (sum(loan.line_ids.filtered(lambda l: l.kind == 'principal').mapped('amount')),
                       len(loan.line_ids))
                loan.write(vals)
                loan.prepayment_ids.unlink()
                loan.line_ids.with_context(nj_no_refresh=True).unlink()
                loan.rate_ids.unlink()
            else:
                vals.update(currency_id=usd.id, match_keys=b['keys'])
                loan = Loan.create(vals)
                old = None
            self.env['nj.loan.rate'].create([
                {'loan_id': loan.id, 'date_from': _d(d), 'rate': r, 'basis': str(bs)} for d, r, bs in b['rates']])
            self.env['nj.loan.line'].with_context(nj_no_refresh=True).create([
                {'loan_id': loan.id, 'date': _d(d), 'kind': k, 'amount': a, 'source': 'excel',
                 'note': b['notes'].get(d) if k == 'adj' else False} for d, k, a in b['events']])
            loan._refresh_status()
            # control: Odoo month-end balances against the file
            rows_d = engine.daily([(_d(d), k, a) for d, k, a in b['events']],
                                  [(_d(d), r, bs) for d, r, bs in b['rates']], base=b['base'])
            by_date = {r['date']: r['balance'] for r in rows_d}
            last_bal = 0.0
            mism = 0
            for d, v in b['month_end'].items():
                dd = _d(d)
                if by_date:
                    if dd > max(by_date):
                        ours = rows_d[-1]['balance']
                    elif dd < min(by_date):
                        ours = 0.0
                    else:
                        ours = by_date[dd]
                else:
                    ours = 0.0
                if abs(ours - v) > 0.005:
                    mism += 1
            msg = Markup("Загружено из Excel «%s»: строк графика %s, корректировок начисления %s, ставок %s.") % (
                self.filename or '', len([e for e in b['events'] if e[1] != 'adj']),
                len([e for e in b['events'] if e[1] == 'adj']), len(b['rates']))
            if old:
                msg += Markup("<br/>До загрузки: строк %s, погашение долга всего %s.") % (
                    old[1], format_amount(self.env, old[0], loan.currency_id))
            msg += Markup("<br/>Контроль остатков на конец месяцев: %s") % (
                "совпадает" if not mism else "расхождений %s" % mism)
            loan.message_post(body=msg)
            rows.append((loan, mism))
        self.env['nj.loan'].match_statement_lines()
        html = Markup("<table class='table table-sm'><thead><tr><th>Код</th><th>Банк</th><th>Договор</th>"
                      "<th style='text-align:right'>Остаток сегодня</th><th>Контроль остатков</th></tr></thead><tbody>")
        for loan, mism in rows:
            html += Markup("<tr><td>%s</td><td>%s</td><td>%s</td><td style='text-align:right'>%s</td><td>%s</td></tr>") % (
                loan.name, loan.bank_id.display_name, loan.contract,
                format_amount(self.env, loan.balance, loan.currency_id),
                "OK" if not mism else "Расхождений: %s" % mism)
        html += Markup("</tbody></table>")
        self.write({'state': 'done', 'result': html, 'file': False})
        return self._reopen()

    def action_open_loans(self):
        return self.env['ir.actions.act_window']._for_xml_id('nj_bank_loans.action_nj_loan')


class NjLoanImportWizardLine(models.TransientModel):
    _name = 'nj.loan.import.wizard.line'
    _description = "Загрузка кредитов: блок файла"
    _order = 'index'

    wizard_id = fields.Many2one('nj.loan.import.wizard', required=True, ondelete='cascade')
    do_import = fields.Boolean("Загрузить", default=True)
    warning = fields.Char("Внимание", readonly=True)
    index = fields.Integer("№")
    label = fields.Char("Название в файле", readonly=True)
    contract = fields.Char("Договор", readonly=True)
    name = fields.Char("Код", required=True)
    loan_id = fields.Many2one('nj.loan', "Обновить кредит")
    partner_id = fields.Many2one('res.partner', "Банк (контрагент)")
    n_events = fields.Integer("Строк", readonly=True)
    n_adj = fields.Integer("Корр.", readonly=True)
    disb_total = fields.Float("Получено", readonly=True, digits=(16, 2))
    last_balance = fields.Float("Остаток в конце файла", readonly=True, digits=(16, 2))
    basis = fields.Char("База", readonly=True)
    rate_text = fields.Char("Ставки", readonly=True)
    accrual_base = fields.Selection([('prev', "Пред. день"), ('same', "Тот же день")], "Проценты от", readonly=True)
    interest_window = fields.Selection([('start', "[пред.; дата)"), ('end', "(пред.; дата]")], "Период %", readonly=True)
