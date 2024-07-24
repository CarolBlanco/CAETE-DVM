
class budget_output:
    """ Helper class to store the output of the daily_budget function.
    """
    def __init__(self, *args):
        fields = ["evavg", "epavg", "phavg", "aravg", "nppavg",
                  "laiavg", "rcavg", "f5avg", "rmavg", "rgavg",
                  "cleafavg_pft", "cawoodavg_pft", "cfrootavg_pft",
                  "stodbg", "ocpavg", "wueavg", "cueavg", "c_defavg",
                  "vcmax", "specific_la", "nupt", "pupt", "litter_l",
                  "cwd", "litter_fr", "npp2pay", "lnc", "delta_cveg",
                  "limitation_status", "uptk_strat", 'cp', 'c_cost_cwm']
        
        for field, value in zip(fields, args):
            setattr(self, field, value)
