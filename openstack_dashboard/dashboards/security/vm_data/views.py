from horizon import tables

from openstack_dashboard.dashboards.security.vm_data \
    import tables as project_tables

class MainIndexView(tables.DataTableView):
    table_class = project_tables.MainTable
    template_name = 'security/vm_data/main.html'
    page_title = "main page"

    def get_data(self):
        data = []
        return data