# -*- coding: utf-8 -*-
# Part of Odoo. See LICENSE file for full copyright and licensing details.

from ast import literal_eval

from odoo import api, fields, models, _
from odoo.exceptions import ValidationError


class Project(models.Model):
    _inherit = 'project.project'

    sale_line_id = fields.Many2one(
        'sale.order.line', 'Sales Order Item', copy=False,
        domain="[('is_service', '=', True), ('is_expense', '=', False), ('order_id', '=', sale_order_id), ('state', 'in', ['sale', 'done']), '|', ('company_id', '=', False), ('company_id', '=', company_id)]",
        help="Sales order item to which the project is linked. Link the timesheet entry to the sales order item defined on the project. "
        "Only applies on tasks without sale order item defined, and if the employee is not in the 'Employee/Sales Order Item Mapping' of the project.")
    sale_order_id = fields.Many2one('sale.order', 'Sales Order',
        domain="[('order_line.product_id.type', '=', 'service'), ('partner_id', '=', partner_id)]",
        copy=False, help="Sales order to which the project is linked.")
    project_overview = fields.Boolean('Show Project Overview', compute='_compute_project_overview')

    _sql_constraints = [
        ('sale_order_required_if_sale_line', "CHECK((sale_line_id IS NOT NULL AND sale_order_id IS NOT NULL) OR (sale_line_id IS NULL))", 'The project should be linked to a sale order to select a sale order item.'),
    ]

    @api.model
    def _map_tasks_default_valeus(self, task, project):
        defaults = super()._map_tasks_default_valeus(task, project)
        defaults['sale_line_id'] = False
        return defaults

    @api.depends('analytic_account_id')
    def _compute_project_overview(self):
        overview = self.env['project.project']
        if self.user_has_groups('analytic.group_analytic_accounting'):
            overview = self.filtered(lambda p: p.analytic_account_id)
            overview.project_overview = True
        (self - overview).project_overview = False

class ProjectTask(models.Model):
    _inherit = "project.task"

    sale_order_id = fields.Many2one('sale.order', 'Sales Order', help="Sales order to which the task is linked.")
    sale_line_id = fields.Many2one(
        'sale.order.line', 'Sales Order Item', domain="[('is_service', '=', True), ('order_partner_id', 'child_of', commercial_partner_id), ('is_expense', '=', False), ('state', 'in', ['sale', 'done']), ('order_id', '=?', project_sale_order_id)]",
        compute='_compute_sale_line', store=True, readonly=False, copy=False,
        help="Sales order item to which the project is linked. Link the timesheet entry to the sales order item defined on the project. "
        "Only applies on tasks without sale order item defined, and if the employee is not in the 'Employee/Sales Order Item Mapping' of the project.")
    project_sale_order_id = fields.Many2one('sale.order', string="Project's sale order", related='project_id.sale_order_id')
    invoice_count = fields.Integer("Number of invoices", related='sale_order_id.invoice_count')
    task_to_invoice = fields.Boolean("To invoice", compute='_compute_task_to_invoice', search='_search_task_to_invoice', groups='sales_team.group_sale_salesman_all_leads')

    @api.depends('project_id.sale_line_id.order_partner_id')
    def _compute_partner_id(self):
        for task in self:
            if not task.partner_id:
                task.partner_id = task.project_id.sale_line_id.order_partner_id
        super()._compute_partner_id()

    @api.depends('commercial_partner_id', 'sale_line_id.order_partner_id.commercial_partner_id', 'parent_id.sale_line_id', 'project_id.sale_line_id')
    def _compute_sale_line(self):
        for task in self:
            if not task.sale_line_id:
                task.sale_line_id = task.parent_id.sale_line_id or task.project_id.sale_line_id
            # check sale_line_id and customer are coherent
            if task.sale_line_id.order_partner_id.commercial_partner_id != task.partner_id.commercial_partner_id:
                task.sale_line_id = False

    @api.constrains('sale_line_id')
    def _check_sale_line_type(self):
        for task in self.sudo():
            if task.sale_line_id:
                if not task.sale_line_id.is_service or task.sale_line_id.is_expense:
                    raise ValidationError(_(
                        'You cannot link the order item %(order_id)s - %(product_id)s to this task because it is a re-invoiced expense.',
                        order_id=task.sale_line_id.order_id.id,
                        product_name=task.sale_line_id.product_id.name,
                    ))

    @api.ondelete(at_uninstall=False)
    def _unlink_except_linked_so(self):
        if any(task.sale_line_id for task in self):
            raise ValidationError(_('You have to unlink the task from the sale order item in order to delete it.'))

    # ---------------------------------------------------
    # Actions
    # ---------------------------------------------------

    def _get_action_view_so_ids(self):
        return self.sale_order_id.ids

    def action_view_so(self):
        self.ensure_one()
        so_ids = self._get_action_view_so_ids()
        action_window = {
            "type": "ir.actions.act_window",
            "res_model": "sale.order",
            "name": "Sales Order",
            "views": [[False, "tree"], [False, "form"]],
            "context": {"create": False, "show_sale": True},
            "domain": [["id", "in", so_ids]],
        }
        if len(so_ids) == 1:
            action_window["views"] = [[False, "form"]]
            action_window["res_id"] = so_ids[0]

        return action_window

    def rating_get_partner_id(self):
        partner = self.partner_id or self.sale_line_id.order_id.partner_id
        if partner:
            return partner
        return super().rating_get_partner_id()

    @api.depends('sale_order_id.invoice_status', 'sale_order_id.order_line')
    def _compute_task_to_invoice(self):
        for task in self:
            if task.sale_order_id:
                task.task_to_invoice = bool(task.sale_order_id.invoice_status not in ('no', 'invoiced'))
            else:
                task.task_to_invoice = False

    @api.model
    def _search_task_to_invoice(self, operator, value):
        query = """
            SELECT so.id
            FROM sale_order so
            WHERE so.invoice_status != 'invoiced'
                AND so.invoice_status != 'no'
        """
        operator_new = 'inselect'
        if(bool(operator == '=') ^ bool(value)):
            operator_new = 'not inselect'
        return [('sale_order_id', operator_new, (query, ()))]


class ProjectTaskRecurrence(models.Model):
    _inherit = 'project.task.recurrence'

    def _new_task_values(self, task):
        values = super(ProjectTaskRecurrence, self)._new_task_values(task)
        task = self.sudo().task_ids[0]
        values['sale_line_id'] = self._get_sale_line_id(task)
        return values

    def _get_sale_line_id(self, task):
        return task.sale_line_id.id
