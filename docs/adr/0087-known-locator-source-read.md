# 0087. Known-locator source reads use read_source and typed locators

Date: 2026-09-24
Status: accepted

The grounded agent keeps `retrieve` for discovery and names the model-facing known-locator read `read_source`, replacing `read_file` when G4 lands because the returned content need not be an entire file. `read_source` may resolve a known valid locator without a same-run `retrieve` hit: the company-corpus locator contract remains valid, while attachment locators use a distinct kind and the host validates attachment identity, active-thread ownership, and parsed-content version; arbitrary filesystem paths are not locators. Only text returned in the current run may support citations (ADR 0046); the exact return unit and size remain open in G4.

Considered Options: keep `read_file` (suggests whole-file access), require a same-run `retrieve` hit for every read (repeats discovery for an already known source), or represent attachments as company-corpus documents (obscures their thread ownership and lifetime).
