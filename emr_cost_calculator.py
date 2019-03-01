#!/usr/bin/env python
"""EMR cost calculator

Usage:
    emr_cost_calculator.py total --region=<reg> \
--created_after=<ca> --created_before=<cb> \
[--aws_access_key_id=<ai> --aws_secret_access_key=<ak>]
    emr_cost_calculator.py cluster --region=<reg> --cluster_id=<ci> \
[--aws_access_key_id=<ai> --aws_secret_access_key=<ak>]
    emr_cost_calculator.py -h | --help


Options:
    -h --help                     Show this screen
    total                         Calculate the total EMR cost \
for a period of time
    cluster                       Calculate the cost of single \
cluster given the cluster id
    --region=<reg>                The aws region that the \
cluster was launched on
    --aws_access_key_id=<ai>      Self-explanatory
    --aws_secret_access_key=<ci>  Self-explanatory
    --created_after=<ca>          The calculator will compute \
the cost for all the cluster created after the created_after day
    --created_before=<cb>         The calculator will compute \
the cost for all the cluster created before the created_before day
    --cluster_id=<ci>             The id of the cluster you want to \
calculate the cost for
"""

from docopt import docopt
import boto3
from retrying import retry
import sys
import datetime
import requests


def validate_date(date_text):
    try:
        return datetime.datetime.strptime(date_text, '%Y-%m-%d')
    except ValueError:
        raise ValueError('Incorrect data format, should be YYYY-MM-DD')


def is_error_retriable(exception):
    """
    Use this function in order to back off only
    if error is retriable
    """
    # TODO verify if this is correct way to handle this. Haven't seen errors
    # TODO like this myself
    try:
        return exception.response['Error']['Code'].startswith("5")
    except AttributeError:
        return False


class Ec2Instance:
    def __init__(self, creation_ts, termination_ts, instance_type, market_type):
        # creation_ts (EMR instance group parameter) correlates to EC2 instance
        # start up time
        self.creation_ts = creation_ts
        self.termination_ts = termination_ts
        self.instance_type = instance_type
        self.market_type = market_type


class InstanceGroup:
    def __init__(self, group_id, instance_type, market_type, group_type):
        self.group_id = group_id
        self.instance_type = instance_type
        self.market_type = market_type
        self.group_type = group_type


class Ec2EmrPricing:
    def __init__(self, region):
        url_base = 'https://pricing.us-east-1.amazonaws.com'

        index_response = requests.get(url_base + '/offers/v1.0/aws/index.json')
        index = index_response.json()

        emr_regions_response = requests.get(url_base + index['offers']['ElasticMapReduce']['currentRegionIndexUrl'])
        emr_region_url = url_base + emr_regions_response.json()['regions'][region]['currentVersionUrl']

        emr_pricing = requests.get(emr_region_url).json()
        sku_to_instance_type = {}
        for sku in emr_pricing['products']:
            if emr_pricing['products'][sku]['attributes']['softwareType'] == 'EMR':
                sku_to_instance_type[sku] = emr_pricing['products'][sku]['attributes']['instanceType']

        self.emr_prices = {}
        for sku in sku_to_instance_type.keys():
            instance_type = sku_to_instance_type.get(sku)
            price = float(emr_pricing['terms']['OnDemand'][sku].itervalues().next()['priceDimensions']
                          .itervalues().next()['pricePerUnit']['USD'])
            self.emr_prices[instance_type] = price

        ec2_regions_response = requests.get(url_base + index['offers']['AmazonEC2']['currentRegionIndexUrl'])
        ec2_region_url = url_base + ec2_regions_response.json()['regions'][region]['currentVersionUrl']

        ec2_pricing = requests.get(ec2_region_url).json()

        ec2_sku_to_instance_type = {}
        for sku in ec2_pricing['products']:
            try:
                if (ec2_pricing['products'][sku]['attributes']['tenancy'] == 'Shared' and
                        ec2_pricing['products'][sku]['attributes']['operatingSystem'] == 'Linux'):
                    ec2_sku_to_instance_type[sku] = ec2_pricing['products'][sku]['attributes']['instanceType']

            except KeyError:
                pass

        self.ec2_prices = {}
        for sku in ec2_sku_to_instance_type.keys():
            instance_type = ec2_sku_to_instance_type.get(sku)
            price = float(ec2_pricing['terms']['OnDemand'][sku].itervalues().next()['priceDimensions']
                          .itervalues().next()['pricePerUnit']['USD'])
            self.ec2_prices[instance_type] = price

    def get_emr_price(self, instance_type):
        return self.emr_prices[instance_type]

    def get_ec2_price(self, instance_type):
        return self.ec2_prices[instance_type]


class EmrCostCalculator:
    def __init__(
            self,
            region,
            aws_access_key_id=None,
            aws_secret_access_key=None):

        try:
            print >> sys.stderr, \
                '[INFO] Retrieving cost in region %s' % region
            self.conn = \
                boto3.client('emr',
                             region_name=region,
                             aws_access_key_id=aws_access_key_id,
                             aws_secret_access_key=aws_secret_access_key
                             )
        except Exception as e:
            print >> sys.stderr, \
                '[ERROR] Could not establish connection with EMR API'
            print(e)
            sys.exit()

        try:
            self.spot_pricing = SpotPricing(region, aws_access_key_id,
                                            aws_secret_access_key)
        except:
            print >> sys.stderr, \
                '[ERROR] Could not establish connection with EC2 API'

        self.ec2_emr_pricing = Ec2EmrPricing(region)

    def get_total_cost_by_dates(self, created_after, created_before):
        total_cost = 0
        for cluster_id in \
                self._get_cluster_list(created_after, created_before):
            cost_dict = self.get_cluster_cost(cluster_id)
            if 'TOTAL' in cost_dict:
                total_cost += cost_dict['TOTAL']
            else:
                print >> sys.stderr, \
                    '[INFO] Cluster %s has no cost associated with it' % cluster_id
        return total_cost

    @retry(
        wait_exponential_multiplier=1000,
        wait_exponential_max=7000,
        retry_on_exception=is_error_retriable
    )
    def get_cluster_cost(self, cluster_id):
        """
        Joins the information from the instance groups and the instances
        in order to calculate the price of the whole cluster

        It is important that we use a backoff policy in this case since Amazon
        throttles the number of API requests.
        :return: A dictionary with the total cost of the cluster and the
                individual cost of each instance group (Master, Core, Task)
        """
        instance_groups = self._get_instance_groups(cluster_id)
        availability_zone = self._get_availability_zone(cluster_id)

        cost_dict = {}
        for instance_group in instance_groups:
            for instance in self._get_instances(instance_group, cluster_id):
                cost = self._get_instance_cost(instance, availability_zone)
                group_type = instance_group.group_type
                cost_dict.setdefault(group_type + ".EC2", 0)
                cost_dict[group_type + ".EC2"] += cost
                cost_dict.setdefault(group_type + ".EMR", 0)
                hours_run = ((instance.termination_ts - instance.creation_ts).total_seconds() / 3600)
                emr_cost = self.ec2_emr_pricing.get_emr_price(instance.instance_type) * hours_run
                cost_dict[group_type + ".EMR"] += emr_cost
                cost_dict.setdefault('TOTAL', 0)
                cost_dict['TOTAL'] += cost + emr_cost

        return cost_dict

    def _get_instance_cost(self, instance, availability_zone):
        if instance.market_type == "SPOT":
            return self.spot_pricing.get_billed_price_for_period(
                instance.instance_type, availability_zone, instance.creation_ts, instance.termination_ts)

        elif instance.market_type == "ON_DEMAND":
            ec2_price = self.ec2_emr_pricing.get_ec2_price(instance.instance_type)
            return ec2_price * ((instance.termination_ts - instance.creation_ts).total_seconds() / 3600)

    def _get_cluster_list(self, created_after, created_before):
        """
        :return: An iterator of cluster ids for the specified dates
        """
        kwargs = {'CreatedAfter': created_after, 'CreatedBefore': created_before}
        while True:
            cluster_list = self.conn.list_clusters(**kwargs)
            for cluster in cluster_list['Clusters']:
                yield cluster['Id']
            try:
                kwargs['Marker'] = cluster_list['Marker']
            except KeyError:
                break

    def _get_instance_groups(self, cluster_id):
        """
        Invokes the EMR api and gets a list of the cluster's instance groups.
        :return: List of our custom InstanceGroup objects
        """
        groups = self.conn.list_instance_groups(ClusterId=cluster_id)['InstanceGroups']
        instance_groups = []
        for group in groups:
            inst_group = InstanceGroup(
                group['Id'],
                group['InstanceType'],
                group['Market'],
                group['InstanceGroupType']
            )

            instance_groups.append(inst_group)
        return instance_groups

    def _get_instances(self, instance_group, cluster_id):
        """
        Invokes the EMR api to retrieve a list of all the instances
        that were used in the cluster.
        This list is then joined to the InstanceGroup list
        on the instance group id
        :return: An iterator of our custom Ec2Instance objects.
        """
        instance_list = []
        list_instances_args = {'ClusterId': cluster_id, 'InstanceGroupId': instance_group.group_id}
        while True:
            batch = self.conn.list_instances(**list_instances_args)
            instance_list.extend(batch['Instances'])
            try:
                list_instances_args['Marker'] = batch['Marker']
            except KeyError:
                break
        for instance_info in instance_list:
            try:
                creation_time = instance_info['Status']['Timeline']['CreationDateTime']
                try:
                    end_date_time = instance_info['Status']['Timeline']['EndDateTime']
                except KeyError:
                    # use same TZ as one in creation time. By default datetime.now() is not TZ aware
                    end_date_time = datetime.datetime.now(tz=creation_time.tzinfo)

                inst = Ec2Instance(
                    instance_info['Status']['Timeline']['CreationDateTime'],
                    end_date_time,
                    instance_group.instance_type,
                    instance_group.market_type
                )
                yield inst
            except AttributeError as e:
                print >> sys.stderr, \
                    '[WARN] Error when computing instance cost. Cluster: %s' \
                    % cluster_id
                print >> sys.stderr, e

    def _get_availability_zone(self, cluster_id):
        cluster_description = self.conn.describe_cluster(ClusterId=cluster_id)
        return cluster_description['Cluster']['Ec2InstanceAttributes']['Ec2AvailabilityZone']


class SpotPricing:
    def __init__(self, region, aws_access_key_id, aws_secret_access_key):
        self.all_prices = {}
        self.client_ec2 = boto3.client(
            'ec2',
            region_name=region,
            aws_access_key_id=aws_access_key_id,
            aws_secret_access_key=aws_secret_access_key)

    def _populate_all_prices_if_needed(self, instance_id, availability_zone, start_time, end_time):
        previous_ts = None

        if (instance_id, availability_zone) in self.all_prices:
            prices = self.all_prices[(instance_id, availability_zone)]
            if (end_time - sorted(prices.keys())[-1] < datetime.timedelta(days=1, hours=1) and
                    sorted(prices.keys())[0] < start_time):
                # this means we already have requested dates. Nothing to do
                return
        else:
            prices = {}

        next_token = ""
        while True:
            prices_response = self.client_ec2.describe_spot_price_history(
                InstanceTypes=[instance_id],
                ProductDescriptions=['Linux/UNIX (Amazon VPC)'],
                AvailabilityZone=availability_zone,
                StartTime=start_time,
                EndTime=end_time,
                NextToken=next_token
            )
            for price in prices_response['SpotPriceHistory']:
                if previous_ts is None:
                    previous_ts = price['Timestamp']
                if previous_ts - price['Timestamp'] > datetime.timedelta(days=1, hours=1):
                    print >> sys.stderr, \
                        "[ERROR] Expecting maximum of 1 day 1 hour difference between spot price entries. Two dates " \
                        "causing problems: %s AND %s Diff is: %s" % (
                            previous_ts, price['Timestamp'], previous_ts - price['Timestamp'])
                    quit(-1)
                prices[price['Timestamp']] = float(price['SpotPrice'])
                previous_ts = price['Timestamp']

            next_token = prices_response['NextToken']
            if next_token == "":
                break

        self.all_prices[(instance_id, availability_zone)] = prices

    def get_billed_price_for_period(self, instance_id, availability_zone, start_time, end_time):
        self._populate_all_prices_if_needed(instance_id, availability_zone, start_time, end_time)

        prices = self.all_prices[(instance_id, availability_zone)]

        summed_price = 0.0
        sorted_price_timestamps = sorted(prices.keys())

        summed_until_timestamp = start_time
        for key_id in range(0, len(sorted_price_timestamps)):
            price_timestamp = sorted_price_timestamps[key_id]
            if key_id == len(sorted_price_timestamps) - 1 or end_time < sorted_price_timestamps[key_id + 1]:
                # this is the last price measurement we want: add final part of price segment and exit
                seconds_passed = (end_time - summed_until_timestamp).total_seconds()
                summed_price = summed_price + (float(seconds_passed) * prices[price_timestamp] / 3600.0)
                return summed_price
            if sorted_price_timestamps[key_id] < summed_until_timestamp < sorted_price_timestamps[key_id + 1]:
                seconds_passed = (sorted_price_timestamps[key_id + 1] - summed_until_timestamp).total_seconds()
                summed_price = summed_price + (float(seconds_passed) * prices[price_timestamp] / 3600.0)
                summed_until_timestamp = sorted_price_timestamps[key_id + 1]


if __name__ == '__main__':
    args = docopt(__doc__)
    if args.get('total'):
        created_after_arg = validate_date(args.get('--created_after'))
        created_before_arg = validate_date(args.get('--created_before'))
        calc = EmrCostCalculator(
            args.get('--region'),
            args.get('--aws_access_key_id'),
            args.get('--aws_secret_access_key')
        )
        print "TOTAL COST: %.2f" % (calc.get_total_cost_by_dates(created_after_arg, created_before_arg))

    elif args.get('cluster'):
        calc = EmrCostCalculator(
            args.get('--region'),
            args.get('--aws_access_key_id'),
            args.get('--aws_secret_access_key')
        )
        calculated_prices = calc.get_cluster_cost(args.get('--cluster_id'))
        for key in sorted(calculated_prices.keys()):
            print "%12s: %6.2f" % (key, calculated_prices[key])
    else:
        print >> sys.stderr, \
            '[ERROR] Invalid operation, please check usage again'
