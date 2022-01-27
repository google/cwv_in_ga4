#!/usr/bin/env python3
import argparse
import os
from typing import List
import google.auth
from google.auth.credentials import Credentials
import google.api_core.exceptions
from googleapiclient import discovery
from google.cloud import bigquery_datatransfer

# Copyright 2021 Google LLC
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#     https://www.apache.org/licenses/LICENSE-2.0
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

# Deploys the SQL tables and scripts needed to collect CWVs according to the
# standard set in https://web.dev/vitals-ga4/ as well as a cloud run function
# for alerting.


def get_gcp_regions(credentials: Credentials, project_id: str) -> List[str]:
  """Fetches the list of available GCP regions and returns a list of str.

  Args:
    credentials: the Google credentials to use to authenticate.
    project: The project to use when making the query.

  Returns:
    A list of region names in str format.
  """
  regions = []
  service = discovery.build('compute', 'v1', credentials=credentials)
  request = service.regions().list(project=project_id)
  while request is not None:
    response = request.execute()
    for region in response['items']:
      if 'name' in region and region['name'] != '':
        regions.append(region['name'])

    if 'nextPageToken' in response:
      request = service.regions().list(pageToken=response['nextPageToken'])
    else:
      request = None

  return regions


def delete_scheduled_query(display_name: str, project_id: str, region: str):
  """Deletes the BigQuery scheduled queries (data transfer) with the given
  display name.

  Please note that the display name of a BigQuery scheduled query is not unique.
  This means that multiple queries can be deleted.

  Args:
    display_name: the name of the config to delete.
    project_id: the project to delete the query from.
    region: the region the query is stored in.
  """
  transfer_client = bigquery_datatransfer.DataTransferServiceClient()
  parent = transfer_client.common_location_path(project=project_id,
                                                location=region)
  transfer_config_req = bigquery_datatransfer.ListTransferConfigsRequest(
    parent=parent,
    data_source_ids=['scheduled_query'])
  configs = transfer_client.list_transfer_configs(request=transfer_config_req)
  for config in configs:
    if config.display_name == display_name:
      transfer_client.delete_transfer_config(name=config.name)


def deploy_scheduled_materialize_query(
    project_id: str,
    region: str,
    ga_property: str) -> None:
  """Deploys the query to create the materialized CWV summary table.

  The scheduled query is given the name "Update Web Vitals Summary" and any
  other scheduled query with this name will be deleted before the new one is
  deployed.

  Args:
    project_id: The project to deploy the query to.
    region: the region of the dataset used for the GA export.
    ga_property: The GA property used to collect the CWV data.
  """
  display_name = 'Update Web Vitals Summary'

  materialize_query = f'''
-- Copyright 2021 Google LLC
-- Licensed under the Apache License, Version 2.0 (the "License");
-- you may not use this file except in compliance with the License.
-- You may obtain a copy of the License at

--     https://www.apache.org/licenses/LICENSE-2.0

-- Unless required by applicable law or agreed to in writing, software
-- distributed under the License is distributed on an "AS IS" BASIS,
-- WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
-- See the License for the specific language governing permissions and
-- limitations under the License.

-- Materialize Web Vitals metrics from GA4 event export data

CREATE OR REPLACE TABLE `{project_id}.analytics_{ga_property}.web_vitals_summary`
  PARTITION BY DATE(event_timestamp)
  CLUSTER BY metric_name
AS
SELECT
  ga_session_id,
  IF(
    EXISTS(
      SELECT 1
      FROM UNNEST(events) AS e
      WHERE e.event_name = 'first_visit'
    ),
    'New user',
    'Returning user') AS user_type,
  IF(
    (SELECT MAX(session_engaged) FROM UNNEST(events)) > 0, 'Engaged', 'Not engaged')
    AS session_engagement,
  evt.* EXCEPT (session_engaged, event_name),
  event_name AS metric_name,
  FORMAT_TIMESTAMP('%Y%m%d', event_timestamp) AS event_date
FROM
  (
    SELECT
      ga_session_id,
      ARRAY_AGG(custom_event) AS events
    FROM
      (
        SELECT
          ga_session_id,
          STRUCT(
            country,
            device_category,
            device_os,
            traffic_medium,
            traffic_name,
            traffic_source,
            page_path,
            debug_target,
            event_timestamp,
            event_name,
            metric_id,
            IF(event_name = 'LCP', metric_value / 1000, metric_value)
              AS metric_value,
            user_pseudo_id,
            session_engaged,
            session_revenue) AS custom_event
        FROM
          (
            SELECT
              (SELECT value.int_value FROM UNNEST(event_params) WHERE key = 'ga_session_id')
                AS ga_session_id,
              (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'metric_id')
                AS metric_id,
              ANY_VALUE(device.category) AS device_category,
              ANY_VALUE(device.operating_system) AS device_os,
              ANY_VALUE(traffic_source.medium) AS traffic_medium,
              ANY_VALUE(traffic_source.name) AS traffic_name,
              ANY_VALUE(traffic_source.source) AS traffic_source,
              ANY_VALUE(
                REGEXP_SUBSTR(
                  (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'page_location'),
                  r'^[^?]+')) AS page_path,
              ANY_VALUE(
                (SELECT value.string_value FROM UNNEST(event_params) WHERE key = 'debug_target'))
                AS debug_target,
              ANY_VALUE(user_pseudo_id) AS user_pseudo_id,
              ANY_VALUE(geo.country) AS country,
              ANY_VALUE(event_name) AS event_name,
              SUM(ecommerce.purchase_revenue) AS session_revenue,
              MAX(
                (
                  SELECT
                    COALESCE(
                      value.double_value,
                      value.int_value,
                      CAST(value.string_value AS NUMERIC))
                  FROM UNNEST(event_params)
                  WHERE key = 'session_engaged'
                )) AS session_engaged,
              TIMESTAMP_MICROS(MAX(event_timestamp)) AS event_timestamp,
              MAX(
                (
                  SELECT COALESCE(value.double_value, value.int_value)
                  FROM UNNEST(event_params)
                  WHERE key = 'metric_value'
                )) AS metric_value,
            FROM
              # Replace source table name
              `{project_id}.analytics_{ga_property}.events_*`
            WHERE
              event_name IN ('LCP', 'FID', 'CLS', 'first_visit', 'purchase')
            GROUP BY
              1, 2
          )
      )
    WHERE
      ga_session_id IS NOT NULL
    GROUP BY ga_session_id
  )
CROSS JOIN UNNEST(events) AS evt
WHERE evt.event_name NOT IN ('first_visit', 'purchase');
  '''

  delete_scheduled_query(display_name=display_name, project_id=project_id,
                         region=region)

  transfer_client = bigquery_datatransfer.DataTransferServiceClient()
  parent = transfer_client.common_location_path(project=project_id,
                                                location=region)
  transfer_config = bigquery_datatransfer.TransferConfig(
    display_name=display_name,
    data_source_id='scheduled_query',
    params={
      'query': materialize_query,
    },
    schedule='every 24 hours',
  )

  transfer_config = transfer_client.create_transfer_config(
    bigquery_datatransfer.CreateTransferConfigRequest(
      parent=parent,
      transfer_config=transfer_config
    )
  )


def deploy_p75_procedure():
  pass


def deploy_cloudrun_alerter():
  pass


def create_cloudrun_trigger():
  pass


def main():
  """The main entry point.

  Command line arguments are parsed and any missing information is gathered
  before running through the deployment steps.
  """
  arg_parser = argparse.ArgumentParser(
    description='Deploys the CWV in GA solution')
  arg_parser.add_argument('-g', '--ga-property', type=int,
                          help=('The GA property ID to use when looking for '
                                'exports in big query.'))
  arg_parser.add_argument('-r', '--region',
                          help='The region GA data is being exported to.')
  arg_parser.add_argument('-l', '--lcp-threshold', default=2500,
                          help=('The value to use as the threshold for a good '
                                'LCP score in ms (default %(default)d).'))
  arg_parser.add_argument('-f' '--fid-threshold', default=100,
                          help=('The value to use as a threshold for a good FID'
                                ' score in ms (default %(default)d)'))
  arg_parser.add_argument('-c', '--cls-threshold', default=0.1,
                          help=('The value to use as a threshold for a good CLS'
                                ' score (unit-less)(default %(default)1.1f)'))
  arg_parser.add_argument('-s', '--email-server',
                          help=('The address of the email server to use to send'
                                ' alerts.'))
  arg_parser.add_argument('-u', '--email-user',
                          help=('The username to use to authenticate with the '
                                'email server.'))
  arg_parser.add_argument('-p', '--email-password',
                          help=('The password to use to authenticate with the '
                                'email server'))
  arg_parser.add_argument('-a', '--alert-recipients',
                          help=('A comma-separated list of email addresses to '
                                'send the alerts to.'))

  args = arg_parser.parse_args()

  credentials, project_id = google.auth.default()
  if project_id is None or project_id == '':
    project_id = os.environ['GOOGLE_CLOUD_PROJECT']

  if not args.region:
    args.region = input(
      'Which region should be deployed to (type list for a list)? ').strip()
    while args.region == 'list':
      region_list = get_gcp_regions(credentials, project_id)
      print('\n'.join(region_list))
      args.region = (input(
        'Which region is the GA export in (list for a list of regions)? ')
        .strip())
  if not args.ga_property:
    args.ga_property = (input(
      'Please enter the GA property ID you are collecting CWV data with: ')
      .strip())
    if not args.ga_property.isdigit():
        raise SystemExit('Only GA4 properties are supported at this time.')

  if not args.email_server:
    args.email_server = (input(
      'Please enter the address of the email server to use to send alerts: ')
      .strip())
  if not args.email_user:
    args.email_user = (input(
      'Please enter the username for authenticating with the email server: ')
      .strip())
  if not args.email_password:
    args.email_password = (input(
      'Please enter the password for authenticating with the email server: ')
      .strip())
  if not args.alert_recipients:
    args.alert_recipients = (input(
      'Please enter a comma-separated list of email addresses to send the '
      'alerts to: ')).strip()

  deploy_scheduled_materialize_query(project_id, args.region, args.ga_property)
  deploy_p75_procedure()
  deploy_cloudrun_alerter()
  create_cloudrun_trigger()


if __name__ == '__main__':
  main()