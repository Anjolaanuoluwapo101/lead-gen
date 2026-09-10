#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""BusinessSource adapter for DataForSEO's Google Maps SERP endpoint."""

import dataforseo

from .base import BusinessSource


class DataForSEO(BusinessSource):
    name = "dataforseo"

    def __init__(self, *, login=None, password=None):
        # login/password may be None -> dataforseo.scrape_maps falls back to its
        # own module globals (master env creds). Passing them lets a per-seller
        # pair override the master for THIS adapter instance.
        self.login = login
        self.password = password

    def find_businesses(self, keyword, place, *, location_name=None,
                        location_code=None, max_results=10):
        rows, _ = dataforseo.scrape_maps(
            keyword, place, location_name=location_name,
            location_code=location_code, max_results=max_results,
            login=self.login, password=self.password)
        return rows
