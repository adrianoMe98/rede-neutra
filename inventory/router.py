INVENTORY_DB = "inventory"


class InventoryRouter:
    def db_for_read(self, model, **hints):
        if model._meta.app_label == "inventory":
            return INVENTORY_DB

    db_for_write = db_for_read

    def allow_migrate(self, db, app_label, **hints):
        return (db == INVENTORY_DB) == (app_label == "inventory")
