from io import BytesIO

from xlsxwriter import Workbook

from .olt_inventory import query_olt


def inventory_workbook(provider, targets):
    output = BytesIO()
    with Workbook(output, {"in_memory": True}) as workbook:
        inventory = workbook.add_worksheet("ONTs e ONUs")
        summary = workbook.add_worksheet("Consultas")
        header = workbook.add_format({"bold": True, "bg_color": "#163A31", "font_color": "#FFFFFF"})
        inventory_columns = (
            ("Provedor", 24), ("OLT", 28), ("VLAN de serviço", 17), ("PON", 12), ("ONT ID", 10),
            ("Service-ports", 19), ("Descrição", 45), ("Serial GPON", 24), ("MAC de autenticação", 23),
            ("MACs na tabela da OLT", 48), ("Detalhe da configuração", 42),
        )
        summary_columns = (
            ("Provedor", 24), ("OLT", 28), ("VLAN de serviço", 17), ("Resultado", 16),
            ("ONTs", 10), ("Detalhe", 68),
        )
        for sheet, columns in ((inventory, inventory_columns), (summary, summary_columns)):
            # A CLI e os cadastros fornecem texto, inclusive quando começa com "=".
            sheet.add_write_handler(str, type(sheet).write_string)
            sheet.write_row(0, 0, [label for label, _ in columns], header)
            for col, (_, width) in enumerate(columns):
                sheet.set_column(col, col, width)
            sheet.freeze_panes(1, 0)

        row_index = summary_index = 0
        for olt, vlans in targets:
            for result in query_olt(olt, vlans):
                summary_index += 1
                summary.write_row(summary_index, 0, (
                    provider.name, olt.name, result.vlan, result.status, len(result.rows), result.detail,
                ))
                for row in result.rows:
                    row_index += 1
                    frame, slot, pon, ont_id = row.position
                    inventory.write_row(row_index, 0, (
                        provider.name, olt.name, result.vlan, f"{frame}/{slot}/{pon}", ont_id,
                        ", ".join(map(str, row.service_ports)), row.ont.description, row.ont.serial,
                        row.ont.mac, ", ".join(row.learned_macs), row.detail,
                    ))
        inventory.autofilter(0, 0, row_index, len(inventory_columns) - 1)
        summary.autofilter(0, 0, summary_index, len(summary_columns) - 1)
    output.seek(0)
    return output
