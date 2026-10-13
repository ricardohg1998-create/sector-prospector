# Public operation guide

Start with README.md and the offline demonstration. `python -m sistema_prospeccion --help` lists all commands; each command has its own `--help`.

## Empty run

```sh
python -m sistema_prospeccion init --sector "Example sector" --run work/my-run --fixture
python -m sistema_prospeccion status --run work/my-run
python -m sistema_prospeccion export --run work/my-run --csv-only
```

An empty run starts no collection. `--fixture` marks outputs as fictional. Use a new path for a new run; initialisation refuses to overwrite existing state.

## Sources and review

Sources require a specification describing kind, location, scope and publication evidence. Importing a file preserves provenance; it does not automatically establish website ownership or a commercial need. Use explicit review commands for unresolved identity, independence and exclusions.

`campaign-prepare` opens or resumes a sector catalogue; `campaign-status` reads state. Network execution and source-route execution require explicit enablement. Test the recipe against saved synthetic pages before using live sources.

## CRM

`campaign-crm-request` produces request envelopes and `campaign-crm-apply` imports response envelopes. A caller must execute them through a compatible, authorised CRM connector. The public engine contains no connector key or deployed CRM endpoint.

## Deliveries

CSV/JSON work with the Python standard library. Install requirements-excel.txt for XLSX. Exporting contacts does not send messages, activate a campaign or prove that recipients are eligible for outreach.
