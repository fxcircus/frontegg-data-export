"""
Frontegg Data Export — full read-only export of a Frontegg environment.

Exports the complete state of your Frontegg environment:
    - tenants (accounts)
    - hierarchy trees (reseller-rooted)
    - users (with tenant memberships, metadata, vendorMetadata)
    - role catalog (each role's permissions[])
    - permission catalog
    - plans (enriched — includes assignedTenantsCount, assignedUsersCount, featuresCount)
    - features
    - feature flags
    - per-(user, tenant) role assignments
    - per-(tenant or user, plan) entitlement assignments

Run it with:  python3 -m frontegg_data_export

Dependencies: Python 3.10+ stdlib only. No `pip install` step.

Safety: only `GET` requests (and a single `POST /auth/vendor/` for
authentication). It does NOT create, update, or delete any Frontegg resource.
"""

__version__ = "1.0.0"
