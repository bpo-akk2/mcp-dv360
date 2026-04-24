"""Google Bid Manager API v2 client for performance metrics and reporting."""

import csv
import io
import json
import logging
import time
from typing import Any, Dict, List, Optional
import asyncio
from concurrent.futures import ThreadPoolExecutor

import requests as http_requests

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google.oauth2.service_account import Credentials as ServiceAccountCredentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .config import Config

logger = logging.getLogger(__name__)

class BidManagerClient:
    """Client for interacting with Google Bid Manager API v2 for performance metrics."""
    
    def __init__(self, config: Config):
        self.config = config
        self.service = None
        self.executor = ThreadPoolExecutor(max_workers=4)
        # Bid Manager API v2 service endpoint
        self.service_name = 'doubleclickbidmanager'
        self.version = 'v2'
    
    async def _get_service(self):
        """Get authenticated Bid Manager service client."""
        if self.service is None:
            def _build_service():
                credentials = self._get_credentials()
                return build(
                    self.service_name,
                    self.version,
                    credentials=credentials,
                    cache_discovery=False
                )
            
            self.service = await asyncio.get_event_loop().run_in_executor(
                self.executor, _build_service
            )
        return self.service
    
    def _get_credentials(self):
        """Get Google API credentials based on configuration."""
        cred_type = self.config.get_credentials_type()
        
        if cred_type == "service_account":
            return ServiceAccountCredentials.from_service_account_file(
                self.config.google_application_credentials,
                scopes=['https://www.googleapis.com/auth/doubleclickbidmanager']
            )
        elif cred_type == "oauth":
            return Credentials(
                token=None,
                refresh_token=self.config.oauth_refresh_token,
                token_uri="https://oauth2.googleapis.com/token",
                client_id=self.config.oauth_client_id,
                client_secret=self.config.oauth_client_secret,
                scopes=['https://www.googleapis.com/auth/doubleclickbidmanager']
            )
        else:
            raise ValueError("No valid credentials configured")
    
    async def _execute_request(self, request):
        """Execute API request asynchronously."""
        def _execute():
            try:
                return request.execute()
            except HttpError as e:
                logger.error(f"Bid Manager API request failed: {e}")
                raise
        
        return await asyncio.get_event_loop().run_in_executor(
            self.executor, _execute
        )
    
    async def run_query(self, query_id: str) -> Dict[str, Any]:
        """Trigger report generation for an existing query (POST queries/{queryId}:run)."""
        service = await self._get_service()
        request = service.queries().run(queryId=query_id, body={})
        return await self._execute_request(request)

    async def _poll_for_report(self, query_id: str, timeout_seconds: int = 120, poll_interval: int = 5) -> Dict[str, Any]:
        """Poll get_report_data() until a report is available or timeout is reached.

        Returns the report dict from get_report_data(), which will either contain
        parsed CSV data or a timeout indicator.
        """
        deadline = time.monotonic() + timeout_seconds
        attempt = 0
        while time.monotonic() < deadline:
            attempt += 1
            logger.info(f"Polling for report (query_id={query_id}, attempt={attempt}) ...")
            report = await self.get_report_data(query_id, _skip_auto_run=True)
            # A report is ready when csv_data or google_cloud_storage_path is present
            # and the status is not "No report data available yet"
            if report.get('csv_data') is not None or (
                report.get('google_cloud_storage_path') and
                report.get('status') != 'No report data available yet'
            ):
                logger.info(f"Report ready after {attempt} poll attempt(s) (query_id={query_id})")
                return report
            if report.get('error'):
                logger.warning(f"Error while polling for report: {report['error']}")
                return report
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            await asyncio.sleep(min(poll_interval, remaining))

        logger.warning(f"Report generation timed out after {timeout_seconds}s (query_id={query_id})")
        return {
            'status': 'still_processing',
            'query_id': query_id,
            'message': (
                f'Report is still being generated after {timeout_seconds}s. '
                f'Use get_performance_report_data with query_id={query_id} to check again later.'
            )
        }

    # ===== PERFORMANCE REPORTING FUNCTIONS =====

    async def create_performance_query(self, advertiser_id: str, campaign_id: Optional[str] = None,
                                     line_item_id: Optional[str] = None,
                                     date_range: str = "LAST_30_DAYS",
                                     metrics: Optional[List[str]] = None) -> Dict[str, Any]:
        """Create a performance query for getting impressions, clicks, CTR, etc."""
        service = await self._get_service()
        
        # Default performance metrics
        if metrics is None:
            metrics = [
                "METRIC_IMPRESSIONS",
                "METRIC_CLICKS", 
                "METRIC_CTR",
                "METRIC_TOTAL_CONVERSIONS",
                "METRIC_MEDIA_COST_USD",
                "METRIC_REVENUE_USD",
                "METRIC_BILLABLE_COST_USD"
            ]
        
        # Build filters
        filters = [
            {
                "type": "FILTER_ADVERTISER",
                "value": advertiser_id
            }
        ]
        
        if campaign_id:
            filters.append({
                "type": "FILTER_CAMPAIGN",
                "value": campaign_id
            })

        if line_item_id:
            filters.append({
                "type": "FILTER_LINE_ITEM",
                "value": line_item_id
            })

        query_body = {
            "metadata": {
                "title": f"Performance Report - {advertiser_id}" + (f" - {campaign_id}" if campaign_id else ""),
                "dataRange": {
                    "range": date_range
                },
                "format": "CSV"  # Changed to CSV format
            },
            "params": {
                "type": "STANDARD",  # Changed to STANDARD type
                "groupBys": ["FILTER_CAMPAIGN", "FILTER_LINE_ITEM"],
                "filters": filters,
                "metrics": metrics
            },
            "schedule": {
                "frequency": "ONE_TIME"
            }
        }
        
        request = service.queries().create(body=query_body)
        return await self._execute_request(request)
    
    async def get_campaign_performance_metrics(self, advertiser_id: str, campaign_id: str, 
                                             date_range: str = "LAST_30_DAYS") -> Dict[str, Any]:
        """Get actual performance metrics for a campaign."""
        try:
            # Create query
            query_response = await self.create_performance_query(
                advertiser_id=advertiser_id,
                campaign_id=campaign_id, 
                date_range=date_range
            )
            
            query_id = query_response.get('queryId')

            if not query_id:
                return {'error': 'Failed to create performance query'}

            # Run the query to trigger report generation
            logger.info(f"Running query {query_id} for campaign performance ...")
            await self.run_query(query_id)

            # Poll until the report is ready (up to 2 minutes)
            report_data = await self._poll_for_report(query_id)

            return {
                'advertiser_id': advertiser_id,
                'campaign_id': campaign_id,
                'date_range': date_range,
                'query_id': query_id,
                'metrics': report_data
            }
            
        except Exception as e:
            logger.error(f"Error getting campaign performance: {e}")
            return {'error': str(e)}
    
    async def get_advertiser_performance_summary(self, advertiser_id: str, 
                                               date_range: str = "LAST_30_DAYS") -> Dict[str, Any]:
        """Get performance summary for entire advertiser."""
        try:
            # Create query for all campaigns under advertiser
            query_response = await self.create_performance_query(
                advertiser_id=advertiser_id,
                date_range=date_range
            )
            
            query_id = query_response.get('queryId')

            if not query_id:
                return {'error': 'Failed to create advertiser performance query'}

            # Run the query to trigger report generation
            logger.info(f"Running query {query_id} for advertiser performance ...")
            await self.run_query(query_id)

            # Poll until the report is ready (up to 2 minutes)
            report_data = await self._poll_for_report(query_id)

            return {
                'advertiser_id': advertiser_id,
                'date_range': date_range,
                'query_id': query_id,
                'summary': report_data
            }
            
        except Exception as e:
            logger.error(f"Error getting advertiser performance: {e}")
            return {'error': str(e)}
    
    async def get_line_item_performance(self, advertiser_id: str, line_item_id: str,
                                      date_range: str = "LAST_30_DAYS") -> Dict[str, Any]:
        """Get performance metrics for a specific line item."""
        service = await self._get_service()
        
        try:
            filters = [
                {
                    "type": "FILTER_ADVERTISER", 
                    "value": advertiser_id
                },
                {
                    "type": "FILTER_LINE_ITEM",
                    "value": line_item_id
                }
            ]
            
            query_body = {
                "metadata": {
                    "title": f"Line Item Performance - {line_item_id}",
                    "dataRange": {
                        "range": date_range
                    },
                    "format": "CSV"  # Changed to CSV format
                },
                "params": {
                    "type": "STANDARD",  # Changed to STANDARD type
                    "groupBys": ["FILTER_LINE_ITEM", "FILTER_CREATIVE"],
                    "filters": filters,
                    "metrics": [
                        "METRIC_IMPRESSIONS",
                        "METRIC_CLICKS",
                        "METRIC_CTR", 
                        "METRIC_TOTAL_CONVERSIONS",
                        "METRIC_MEDIA_COST_USD",
                        "METRIC_REVENUE_USD"
                    ]
                },
                "schedule": {
                    "frequency": "ONE_TIME"
                }
            }
            
            request = service.queries().create(body=query_body)
            query_response = await self._execute_request(request)
            
            query_id = query_response.get('queryId')

            # Run the query to trigger report generation
            logger.info(f"Running query {query_id} for line item performance ...")
            await self.run_query(query_id)

            # Poll until the report is ready (up to 2 minutes)
            report_data = await self._poll_for_report(query_id)

            return {
                'line_item_id': line_item_id,
                'date_range': date_range,
                'query_id': query_id,
                'performance': report_data
            }
            
        except Exception as e:
            logger.error(f"Error getting line item performance: {e}")
            return {'error': str(e)}
    
    async def get_report_data(self, query_id: str, _skip_auto_run: bool = False) -> Dict[str, Any]:
        """Get report data from a query.

        If no reports exist yet and _skip_auto_run is False (the default when called
        via the get_performance_report_data tool), the query is run automatically and
        then polled so the caller gets data without an extra round-trip.

        _skip_auto_run is set to True internally by _poll_for_report to prevent
        recursive polling.
        """
        service = await self._get_service()

        try:
            # First check if query is complete
            query_request = service.queries().get(queryId=query_id)
            query_info = await self._execute_request(query_request)

            # Get reports list
            reports_request = service.queries().reports().list(queryId=query_id)
            reports_response = await self._execute_request(reports_request)

            reports = reports_response.get('reports', [])

            if not reports:
                # If called directly (not from a polling loop), try to run the query
                # then poll for results so the user gets data in one call.
                if not _skip_auto_run:
                    logger.info(f"No reports yet for query {query_id} — running query and polling ...")
                    await self.run_query(query_id)
                    return await self._poll_for_report(query_id)
                return {'status': 'No report data available yet', 'query_status': query_info.get('metadata', {})}

            # Get the latest report
            latest_report = reports[0]  # Reports are ordered by creation time (newest first)
            gcs_path = latest_report.get('metadata', {}).get('googleCloudStoragePath')

            result = {
                'report_id': latest_report.get('key', {}).get('reportId'),
                'query_id': query_id,
                'status': 'Report available',
                'metadata': latest_report.get('metadata', {}),
                'google_cloud_storage_path': gcs_path,
                'report_info': latest_report,
            }

            # Download and parse the CSV if a GCS URL is available.
            # The Bid Manager API returns a direct HTTPS URL
            # (e.g. https://storage.googleapis.com/...) not a gs:// path.
            if gcs_path:
                csv_data = self._download_and_parse_csv(gcs_path)
                result['csv_data'] = csv_data

            return result

        except Exception as e:
            logger.error(f"Error getting report data: {e}")
            return {'error': str(e)}

    def _download_and_parse_csv(self, url: str) -> Optional[List[Dict[str, Any]]]:
        """Download a CSV from the given HTTPS URL and return a list of row dicts.

        Returns None on failure so callers can fall back to the raw GCS path.
        """
        try:
            logger.info(f"Downloading report CSV from {url} ...")
            response = http_requests.get(url, timeout=60)
            response.raise_for_status()
            content = response.text

            # Bid Manager CSV files have a summary trailer after the data rows.
            # The actual data ends before a blank line that precedes the footer.
            # We parse conservatively: use Python's csv module and skip any rows
            # that have fewer columns than the header (footer rows).
            reader = csv.DictReader(io.StringIO(content))
            rows = []
            header_count = None
            for row in reader:
                if header_count is None:
                    header_count = len(reader.fieldnames or [])
                # Skip footer/summary rows (they usually have far fewer values)
                non_empty = sum(1 for v in row.values() if v and v.strip())
                if non_empty < max(1, header_count // 2):
                    break
                rows.append(dict(row))

            logger.info(f"Parsed {len(rows)} rows from report CSV")
            return rows
        except Exception as e:
            logger.error(f"Failed to download/parse report CSV from {url}: {e}")
            return None
    
    async def list_queries(self, page_size: int = 100) -> List[Dict[str, Any]]:
        """List existing queries."""
        service = await self._get_service()
        
        try:
            request = service.queries().list(pageSize=page_size)
            response = await self._execute_request(request)
            return response.get('queries', [])
        except Exception as e:
            logger.error(f"Error listing queries: {e}")
            return []
    
    async def get_query_status(self, query_id: str) -> Dict[str, Any]:
        """Get status of a specific query."""
        service = await self._get_service()
        
        try:
            request = service.queries().get(queryId=query_id)
            return await self._execute_request(request)
        except Exception as e:
            logger.error(f"Error getting query status: {e}")
            return {'error': str(e)}
    
    # ===== AVAILABLE METRICS =====
    
    def get_available_metrics(self) -> List[Dict[str, str]]:
        """Get list of available performance metrics."""
        return [
            {
                'metric': 'METRIC_IMPRESSIONS',
                'description': 'Total ad impressions'
            },
            {
                'metric': 'METRIC_CLICKS',
                'description': 'Number of ad clicks'
            },
            {
                'metric': 'METRIC_CTR',
                'description': 'Click-through rate'
            },
            {
                'metric': 'METRIC_TOTAL_CONVERSIONS',
                'description': 'Total number of conversions'
            },
            {
                'metric': 'METRIC_MEDIA_COST_USD',
                'description': 'Media cost in USD'
            },
            {
                'metric': 'METRIC_BILLABLE_COST_USD',
                'description': 'Billable cost in USD'
            },
            {
                'metric': 'METRIC_REVENUE_USD',
                'description': 'Revenue in USD'
            },
            {
                'metric': 'METRIC_UNIQUE_REACH_IMPRESSION_REACH',
                'description': 'Unique impression reach'
            },
            {
                'metric': 'METRIC_UNIQUE_REACH_CLICK_REACH',
                'description': 'Unique click reach'
            },
            {
                'metric': 'METRIC_IMPRESSIONS_TO_CONVERSION_RATE',
                'description': 'Percentage of impressions leading to conversions'
            }
        ]
    
    def get_available_date_ranges(self) -> List[str]:
        """Get list of available date ranges (Bid Manager API v2 DataRange.Range values)."""
        return [
            "CURRENT_DAY",
            "PREVIOUS_DAY",
            "WEEK_TO_DATE",
            "MONTH_TO_DATE",
            "QUARTER_TO_DATE",
            "YEAR_TO_DATE",
            "PREVIOUS_WEEK",
            "PREVIOUS_MONTH",
            "PREVIOUS_QUARTER",
            "PREVIOUS_YEAR",
            "LAST_7_DAYS",
            "LAST_14_DAYS",
            "LAST_30_DAYS",
            "LAST_60_DAYS",
            "LAST_90_DAYS",
            "LAST_365_DAYS",
            "ALL_TIME"
        ]

    async def create_raw_query(self, body: Dict[str, Any]) -> Dict[str, Any]:
        """Create a query using a raw query body dict, via the Bid Manager API."""
        service = await self._get_service()
        request = service.queries().create(body=body)
        return await self._execute_request(request)