#!/usr/bin/env python
# -*- coding: utf-8 -*-
from __future__ import print_function
import os
import json
import logging
import re

from django.conf import settings
from risk_data_hub import settings as rdh_settings
from django import forms
from django.views.generic import TemplateView, View, FormView
from django.core.urlresolvers import reverse
from django.core.files.storage import default_storage
from django.core.files.base import ContentFile, File
from django.http import HttpResponse, FileResponse
from django.template.loader import render_to_string
from django.utils.crypto import get_random_string
from django.views.decorators.cache import cache_page
from django.core import serializers
from operator import attrgetter

from geonode.layers.models import Layer
from geonode.utils import json_response
from geonode.base.forms import ValuesListField
from risks.models import (HazardType, AdministrativeDivision, Region,
                                          RiskAnalysisDymensionInfoAssociation,
                                          RiskAnalysis, DymensionInfo, AnalysisType,
                                          FurtherResource, RiskApp, Event, AnalysisClass, 
                                          AdministrativeData, AdministrativeDivisionDataAssociation, AdministrativeDivisionMappings)

from risks.datasource import GeoserverDataSource
from risks.pdf_helpers import generate_pdf

from dateutil.parser import parse

from django.apps import apps

cost_benefit_index = TemplateView.as_view(template_name='risks/cost_benefit_index.html')

log = logging.getLogger(__name__) 



class AppAware(object):
    DEFAULT_APP = RiskApp.APP_DATA_EXTRACTION

    def get_app_name(self):
        return self.kwargs['app']

    def get_app(self):
        app_name = self.get_app_name()
        return RiskApp.objects.get(name=app_name)

class ContextAware(AppAware):

    CONTEXT_KEYS = ['ht', 'at', 'an', 'dym']

    def get_context_url(self, **kwargs):
        out = []
        if kwargs.pop('_full', None):
            ctx_keys = ['app', 'loc' ] + self.CONTEXT_KEYS
        else:
            ctx_keys = self.CONTEXT_KEYS
        for k in ctx_keys:
            if kwargs.get(k):
                out.extend([k, kwargs[k]])
            else:
                break
        if out:
            url = '{}/'.format('/'.join(out))
        else:
            url = None
        return url

    def fr_for_an(self, an, **kwargs):
        """
        .. py:method: ft_for_an(an, **kwargs)

        :param an: Risk Analysis object
        :param dict kwargs: other parameters available
        :type an: :py:class: rdh.risks.models.RiskAnalysis

        Returns list of :py:class: rdh.risks.models.FurtherResource
            related to Hazard type (assigned to Risk Analysis). Region may be used to narrow results.

        """
        if an.hazardset is None:
            return []
        region = None
        #if kwargs.get('loc'):
            #region = kwargs['loc'].region
        if kwargs.get('reg'):
            region = kwargs['reg']

        return FurtherResource.for_hazard_set(an.hazardset, region=region)


    def fr_for_dym(self, dym, **kwargs):
        """
        .. py:method: fr_for_dym(dym, **kwargs)

        :param dym: DymensionInfo object
        :param dict kwargs: other parameters for query
        :type dym: :py:class: rdh.risks.models.DymensionInfo

        Returns list of :py:class: rdh.risks.models.FurtherResource
            related to DymensionInfo. Region and Risk Analysis may be used to
            narrow results.
        """


        if dym is None:
            return []
        ranalysis = kwargs.get('an')
        region = None
        #if kwargs.get('loc'):
            #region = kwargs['loc'].region
        if kwargs.get('reg'):
            region = kwargs['reg']
        return FurtherResource.for_dymension_info(dym, region=region, ranalysis=ranalysis)


    def fr_for_at(self, at, **kwargs):
        """
        .. py:method: fr_for_at(dym, **kwargs)

        :param at: AnalysisType object
        :param dict kwargs: other parameters for query
        :type dym: :py:class: rdh.risks.models.DymensionInfo

        Returns list of :py:class: rdh.risks.models.FurtherResource
            related to DymensionInfo. Region and Risk Analysis may be used to
            narrow results.
        """
        if at is None:
            return []
        htype = kwargs.get('ht')
        region = None
        #if kwargs.get('loc'):
            #region = kwargs['loc'].region
        if kwargs.get('reg'):
            region = kwargs['reg']
        return FurtherResource.for_analysis_type(at, region=region, htype=htype)


    # maps url captured argument to specific class and field for lookup
    CONTEXT_KEYS_CLASSES = (('ht', HazardType, 'mnemonic'),
                            ('at', AnalysisType, 'name',),
                            ('an', RiskAnalysis, 'id',),
                            ('dym', DymensionInfo, 'id',),
                            ('reg', Region, 'name',),
                            ('loc', AdministrativeDivision, 'code',)
                            )


    def get_further_resources_inputs(self, **kwargs):
        """
        .. py:method:: get_further_resources_inputs(self, **kwargs)

        :param dict kwargs: keyword arguments obtained from url parser
        :return: dictionary with objects for keyword and criteria

        This will check each pair of (key, value) from url kwargs and,
        using map between key and class, will get specific object identified
        by value.

        """

        out = {}
        for k, klass, field in self.CONTEXT_KEYS_CLASSES:
            if not kwargs.get(k):
                continue
            related = self._get_from_kwargs(klass, field, kwargs[k])
            out[k] = related
        return out

    def get_further_resources(self, **kwargs):
        """
        .. py:method:: get_further_resources(self, **kwargs)

        returns map of criteria and further resources available for given criteria

        :param dict kwargs: keyword arguments obtained from url parser (see CONTEXT_KEY_CLASSES)
        :return: dictionary with object type name and list of related resources
        :rtype: dict

        """
        inputs = kwargs.pop('inputs', None) or self.get_further_resources_inputs(**kwargs)
        out = {}
        for res_type, key_name in (('at', 'analysisType',),
                                    ('dym', 'hazardSet',),
                                    ('an', 'hazardType',)):
            res_type_handler = getattr(self, 'fr_for_{}'.format(res_type))
            if kwargs.get(res_type):
                res_list = res_type_handler(**inputs)
                out[key_name] = self._fr_serialize(res_list)
        return out


    def _fr_serialize(self, items):
        return [i.export() for i in items]

    def _get_from_kwargs(self, klass, field, field_val):
        app = self.get_app()
        kwargs = {field: field_val}
        if hasattr(klass, 'app'):
            kwargs['app'] = app
        return klass.objects.get(**kwargs)


class FeaturesSource(object):

    AXIS_X = 'x'
    AXIS_Y = 'y'
    KWARGS_MAPPING = {'loc': 'adm_code',
                      'ht': 'hazard_type',
                      'an': 'risk_analysis',
                      'evt': 'event_id'}

    def url_kwargs_to_query_params(self, **kwargs):
        out = {}
        for k, v in kwargs.iteritems():
            if self.KWARGS_MAPPING.get(k):
                new_k = self.KWARGS_MAPPING[k]
                out[new_k] = v
        return out

    def get_dim_association(self, analysis, dyminfo):
        ass_list = RiskAnalysisDymensionInfoAssociation.objects.filter(riskanalysis=analysis, dymensioninfo=dyminfo)
        dim_list = set([a.axis_to_dim() for a in ass_list])
        if len(dim_list) != 1:
            raise ValueError("Cannot query more than one dimension at the moment, got {}".format(len(dim_list)))

        return (ass_list.first(), list(dim_list)[0])

    def get_dymlist_field_mapping(self, analysis, dimension, dymlist):
        out = []
        layers = [analysis.layer.typename]
        current_dim_name = self.get_dim_association(analysis, dimension)[1]
        out.append(current_dim_name)
        for dym in dymlist:
            if dym != dimension:
                dim_association = self.get_dim_association(analysis, dym)
                out.append(dim_association[1])
        return (out, layers)

    def get_features(self, analysis, dimension, dymlist, **kwargs):

        (dymlist_to_fields, dym_layers) = self.get_dymlist_field_mapping(analysis, dimension, dymlist)

        s = settings.OGC_SERVER['default']
        gs = GeoserverDataSource('{}/wfs'.format(s['LOCATION'].strip("/")),
                                 username=s['USER'],
                                 password=s['PASSWORD']
                                 )        
        dim_name = dymlist_to_fields[0]        
        layer_name = dym_layers[0]        
        #if 'additional_data' in kwargs:
        #    layer_name = '{}_{}'.format(layer_name, kwargs['additional_data'])
        #features = gs.get_features(layer_name, dim_name, **kwargs)
        features = gs.get_features(layer_name, None, **kwargs)
        return features

    def get_features_base(self, layerName, field_list, **kwargs):
        s = settings.OGC_SERVER['default']
        gs = GeoserverDataSource('{}/wfs'.format(s['LOCATION'].strip("/")),
                                 username=s['USER'],
                                 password=s['PASSWORD']
                                 )
        features = gs.get_features(layerName, field_list, **kwargs)
        return features


class RiskIndexView(AppAware, FeaturesSource, TemplateView):

    TEMPLATES = {RiskApp.APP_DATA_EXTRACTION: 'risks/risk_data_extraction_index.html',
                 RiskApp.APP_COST_BENEFIT: 'risks/cost_benefit_index.html',
                 RiskApp.APP_TEST: 'risks/risk_test_index.html'}

    def get_template_names(self):
        app = self.get_app()
        return [self.TEMPLATES[app.name]]

    def get_context_data(self, *args, **kwargs):
        ctx = super(RiskIndexView, self).get_context_data(*args, **kwargs)
        ctx['app'] = app = self.get_app()

        app_ctx = {'app': app.name,
                   'geometry': app.url_for('geometry', settings.RISKS['DEFAULT_LOCATION']),
                   'region': settings.RISKS['DEFAULT_LOCATION'],
                   'href': app.href}
        ctx['app_ctx'] = json.dumps(app_ctx)

        return ctx


risk_data_extraction_index = RiskIndexView.as_view()
cost_benefit_index = RiskIndexView.as_view()
risk_test_index = RiskIndexView.as_view()


class LocationSource(object):

    def get_region(self, **kwargs):
        try:
            return Region.objects.get(name=kwargs['reg'])            
        except Region.DoesNotExist:
            return

    def get_location_exact(self, loc):
        try:
            return AdministrativeDivision.objects.get(code=loc)            
        except AdministrativeDivision.DoesNotExist:
            return
    
    def get_location(self, **kwargs):
        loc = self.get_location_exact(kwargs['loc'])
        try:
            locations = loc.get_parents_chain() + [loc]
            return locations
        except:
            pass

    def get_location_range(self, loc):
        return AdministrativeDivision.objects.filter(code__in=loc)        
    
    def location_lookup(self, **kwargs):
        matches = AdministrativeDivision.objects.filter(name__contains=kwargs['admlookup'])
        loc_chains = []
        for loc in matches:
            loc_chains.append(loc.get_parents_chain() + [loc])
        return loc_chains      

class LocationView(ContextAware, LocationSource, View):

    def get(self, request, *args, **kwargs):
        reg = self.get_region(**kwargs)
        locations = self.get_location(**kwargs)
        if not locations:
            return json_response(errors=['Invalid location code'], status=404)
        loc = locations[-1]        
        app = self.get_app()
        hazard_types = HazardType.objects.filter(app=app)


        location_data = {'navItems': [location.set_app(app).set_region(reg).export() for location in locations],
                         'context': self.get_context_url(**kwargs),
                         'furtherResources': self.get_further_resources(**kwargs),
                         'overview': [ht.set_region(reg).set_location(loc).export() for ht in hazard_types]}

        return json_response(location_data)

class AdmLookupView(ContextAware, LocationSource, View):
    def prepare_data(self, resultset, location, rtype = 'risk_analysis'):
        analysisData = []
        for r in resultset:            
            if(rtype == 'risk_analysis'):
                loc = location.code
                ht = r.hazard_type.mnemonic
                at = r.analysis_type.name
                an = r.id
                analysisData.append({
                    'riskAnalysis': {'id': r.id, 'name': r.name},
                    'analysisType': at,
                    'hazardType': ht,
                    'admCode': loc,
                    'admName': location.name,
                    'apiUrl': '/risks/data_extraction/loc/{}/ht/{}/at/{}/an/{}/'.format(loc, ht, at, an)
                })
        return analysisData
    
    def get(self, request, *args, **kwargs):
        lookup_data = []
        if 'detail' in kwargs:
            loc_chain = self.get_location(**kwargs)
            if not loc_chain:
                return json_response(errors=['Invalid location code'], status=404)
            
            loc = loc_chain[-1]
            lookup_data = []
            ra_ids = []
            while loc is not None:
                ra_matches = RiskAnalysis.objects.filter(administrative_divisions=loc).exclude(pk__in=ra_ids)                
                if ra_matches:
                    ra_ids += list(ra_matches.values_list('pk', flat=True))
                    lookup_data += self.prepare_data(ra_matches, loc)                
                loc = loc.parent                       
            
        else:
            loc_chains = self.location_lookup(**kwargs)
            if not loc_chains:
                return json_response(errors=['Invalid location code'], status=404)
                        
            for loc_chain in loc_chains:
                current_loc = loc_chain[-1] 
                country = next((x for x in loc_chain if x.level == 1), None)    
                current_chain_data = {
                    'admCode': current_loc.code,
                    'admName': current_loc.name,
                    'country': country.code if country is not None else ''
                }
                lookup_data.append(current_chain_data)
        
        return json_response(lookup_data)                               
                

class HazardTypeView(ContextAware, LocationSource, View):
    """
    loc/AF/ht/EQ/"
{
 "navItems": [{
  "label": "Afghanistan",
  "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF/ht/EQ/at/loss_impact/"
 }, {
  "label": "Badakhshan",
  "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF15/ht/EQ/at/loss_impact/",
 }],
 "overview": [{
  "mnemonic": "EQ",
  "title": "Earthquake",
  "riskAnalysis": 2,
  "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF15/ht/EQ/at/loss_impact/",
 }, {
  "mnemonic": "FL",
  "title": "River Flood",
  "riskAnalysis": 0,
  "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF15/ht/FL/at/loss_impact/"
 }],
    "hazardType": {
        "mnemonic": "EQ",
        "description": "Lorem ipsum dolor, .....",
        "analysisTypes"[{
            "name": "loss_impact",
            "title": "Loss Impact",
            "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF15/ht/EQ/at/loss_impact/"
        }, {
            "name": "impact",
            "title": "Impact Analysis",
            "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF15/ht/EQ/at/impact/"
        }]
    },
    "analysisType":{
        "name": "impact",
        "description": "Lorem ipsum dolor, .....",
        "riskAnalysis": [{
            "name": "WP6_future_proj_Hospital",
            "hazardSet": {
                "title": "Afghanistan Hazard-Exposures for provinces and districts for affected hospitals in future projections for SSPs 1-5 in 2050.",
                "abstract": "This table shows the aggregated results of affected hospitals for the Afghanistan districts and provinces from 1km resolution results in the locations over PGA=0.075g. These are measured in USD. The results are created as future projections for SSPs 1-5 in 2050.",
                "category": "economic",
                "fa_icon": "fa_economic"
            },
            "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF15/ht/EQ/at/impact/an/1/"
        }, {
            ...,
            "href": "http://disasterrisk-af.geo-solutions.it/risks/risk_data_extraction/loc/AF15/ht/EQ/at/impact/an/2/"
        }
        ]
    }



    """

    def get_hazard_type(self, region, location, **kwargs):
        app = self.get_app()
        try:
            return HazardType.objects.get(mnemonic=kwargs['ht'], app=app).set_region(region).set_location(location)
        except (KeyError, HazardType.DoesNotExist,):
            return

    def get_analysis_type(self, region, location, hazard_type, **kwargs):
        atypes = hazard_type.get_analysis_types()
        aclasses = AnalysisClass.objects.all()
        aclass_risk = aclasses.get(name='risk')
        aclass_event = aclasses.get(name='event')
        if not atypes.exists():
            return None, None, None, None
        
        first_atype = atypes.filter(analysis_class=aclass_risk).first()
        if first_atype is not None:
            first_atype = first_atype.set_region(region).set_location(location).set_hazard_type(hazard_type)
        first_atype_e = atypes.filter(analysis_class=aclass_event).first()
        if first_atype_e is not None:
            first_atype_e = first_atype_e.set_region(region).set_location(location).set_hazard_type(hazard_type)
        if not kwargs.get('at'):
            atype_r = first_atype
            atype_e = first_atype_e
            aclass = None
        else:
            atype = atypes.filter(name=kwargs['at']).first()
            if atype is None:
                return None, None, atypes, None
            else:
                atype = atype.set_region(region).set_location(location).set_hazard_type(hazard_type)            
                atype_r = atype if atype.analysis_class == aclass_risk else first_atype
                atype_e = atype if atype.analysis_class == aclass_event else first_atype_e
                aclass = atype.analysis_class
        return atype_r, atype_e, atypes, aclass,

    def get(self, request, *args, **kwargs):
        reg = self.get_region(**kwargs)
        locations = self.get_location(**kwargs)
        if not locations:
            return json_response(errors=['Invalid location code'], status=404)
        loc = locations[-1]
        app = self.get_app()
        hazard_types = HazardType.objects.filter(app=app)

        hazard_type = self.get_hazard_type(reg, loc, **kwargs)

        if not hazard_type:
            return json_response(errors=['Invalid hazard type'], status=404)

        (atype_r, atype_e, atypes, aclass,) = self.get_analysis_type(reg, loc, hazard_type, **kwargs)
                
        if not atype_r and not atype_e:
            return json_response(errors=['No analysis type available for location/hazard type'], status=404)        

        out = {
            'navItems': [location.set_app(app).set_region(reg).export() for location in locations],
            'overview': [ht.set_region(reg).set_location(loc).export() for ht in hazard_types],
            'context': self.get_context_url(**kwargs),
            'furtherResources': self.get_further_resources(**kwargs),
            'hazardType': hazard_type.get_hazard_details(),            
            'analysisType': atype_r.get_analysis_details() if atype_r else {},
            'analysisTypeE': atype_e.get_analysis_details() if atype_e else {}
        }

        return json_response(out)


class EventView(FeaturesSource, HazardTypeView):
    def get_events(self, **kwargs):        
        ids = kwargs['evt'].split('__')
        return Event.objects.filter(pk__in=ids)        

    def get_current_adm_level(self, **kwargs):
        return kwargs['lvl']

    def get(self, request, *args, **kwargs):
        locations = self.get_location(**kwargs)
        app = self.get_app()
        if not locations:
            return json_response(errors=['Invalid location code'], status=404)
        loc = locations[-1]

        #hazard_type = self.get_hazard_type(loc, **kwargs)
        try:
            risk_analysis = RiskAnalysis.objects.get(id=kwargs['an'])
        except RiskAnalysis.DoesNotExist:
            return json_response(errors=['Invalid risk analysis'], status=404)

        events = self.get_events(**kwargs)
        if not events:
            return json_response(errors=['Invalid event Id(s)'], status=404)

        adm_level = self.get_current_adm_level(**kwargs)

        #if not hazard_type:
            #return json_response(errors=['Invalid hazard type'], status=404)
        
        wms_events = {
            'style': None,
            'viewparams': self.get_viewparams(adm_level, risk_analysis, events),
            'baseurl': settings.OGC_SERVER['default']['PUBLIC_LOCATION']            
        }

        layer_style = {
            'name': 'monochromatic',
            'title': None,
            'url': 'http://localhost:8080/geoserver/rest/styles/monochromatic.sld'
        }

        layer_events = {
            'layerName': 'geonode:risk_analysis_event_location',
            'layerStyle': layer_style,
            'layerTitle': 'risk_analysis_event_location'
        }  
        
        related_layers_events = [(l.id, l.typename, l.title, ) for l in events.first().related_layers.all()] if events.count() == 1 else []
        
        feat_kwargs = self.url_kwargs_to_query_params(**kwargs)
        features = self.get_features_base('geonode:risk_analysis', None, **feat_kwargs)
        
        values = [[f['properties']['dim1_value'], f['properties']['dim2_value'], f['properties']['value']] for f in features['features']]


        out = {
            'wms': wms_events,
            'layer': layer_events,
            'relatedLayers': related_layers_events,
            'eventValues': values
        }        

        return json_response(out)

    def get_viewparams(self, adm_level, risk_analysis, events):
        event_ids = '__'.join([e.event_id for e in events])
        
        actual_geom_lookup = int(adm_level) > 1
        target_level = int(adm_level) if actual_geom_lookup else int(adm_level) + 1

        adm_codes_list = []         
        for event in events:
            for adm in event.administrative_divisions.all():                
                if(adm.level == target_level):
                    adm_codes_list.append(adm.code)
        adm_codes = "__".join(list(set(adm_codes_list)))        
        return 'adm_codes:{};risk_analysis:{};event_ids:{};actual_geom_lookup:{}'.format(adm_codes, risk_analysis.name, event_ids, actual_geom_lookup)


class DataExtractionView(FeaturesSource, HazardTypeView):
    """

{
    "riskAnalysisData": {
        "name": "",
        "descriptorFile": "",
        "dataFile": "",
        "metadataFile": "",
        "hazardSet": {
            "title": "",
            "abstract": "",
            "purpose": "",
            "category": "",
            ... other metadata ...
        },
        "data": {
            "dimensions": [
                {
                    "name": "Scenario",
                    "abstract": "Lorem ipsum dolor,...",
                    "unit": "NA",
                    "values": [
                        "Hospital",
                        "SSP1",
                        "SSP2",
                        "SSP3",
                        "SSP4",
                        "SSP5"
                    ]
                },
                {
                    "name": "Round Period",
                    "abstract": "Lorem ipsum dolor,...",
                    "unit": "Years",
                    "values": [
                        "10",
                        "20",
                        "50",
                        "100",
                        "250",
                        "500",
                        "1000",
                        "2500"
                    ]
                }
            ],
            "values":[
                ["Hospital","10",0.0],
                ["Hospital","20",0.0],
                ["Hospital","50",0.0],
                ["Hospital","100",0.0],
                ["Hospital","250",6000000.0],
                ["Hospital","500",6000000.0],
                ["Hospital","1000",6000000.0],
                ["Hospital","2500",6000000.0],

                ["SSP1","10",0.0],
                ["SSP1","20",0.0],
                ["SSP1","50",0.0],
                ["SSP1","100",64380000.0],
                ["SSP1","250",64380000.0],
                ["SSP1","500",64380000.0],
                ["SSP1","1000",64380000.0],
                ["SSP1","2500",64380000.0],

                ...
            ]
        }
    }
}

    """

    def reformat_features(self, risk, dimension, dimensions, features, capitalize=False):
        """
        Returns risk data as proper structure

        """
        values = []
        dims = [dimension.set_risk_analysis(risk)] + [d.set_risk_analysis(risk) for d in dimensions if d.id != dimension.id]

        _fields = [self.get_dim_association(risk, d) for d in dims]
        fields = ['{}_value'.format(f[1]) for f in _fields]
        field_orders = ['{}_order'.format(f[1]) for f in _fields]

        orders = [dict(d.get_axis_order()) for d in dims]

        orders_len = len(orders)

        def make_order_val(feat):
            """
            compute order value
            """
            _order_vals = []

            for idx, o in enumerate(orders):
                field_name = field_orders[idx]
                val = feat['properties'].get(field_name)
                # order_val = o.get(val)
                order_val = val

                if order_val is None:
                    order_val = 0
                # 111 > 1, 1, 1
                # mag = 10 ** (orders_len - idx)
                mag = 1000 if idx == 0 else 1
                _order_vals.append(int('{}'.format(order_val * mag)))
            # return ''.join(_order_vals)
            return sum(_order_vals)

        def order_key(val):
            # order by last val
            order = val.pop(-1)
            return order

        for feat in features:
            p = feat['properties']
            line = []
            [line.append(p[f]) for f in fields]
            line.append(p['value'])
            line.append(make_order_val(feat))
            if capitalize:
                line = [str(item).capitalize() for item in line]
            values.append(line)

        values.sort(key=order_key)

        out = {'dimensions': [dim.set_risk_analysis(risk).export() for dim in dims],
               'values': values}

        return out

    def is_user_allowed(self, request, risk_analysis):
        result = True
        if risk_analysis.owner:
            owner_groups = risk_analysis.owner.groups.all()        
            current_user = request.user
            current_user_group_ids = current_user.groups.all().values_list('id', flat=True)

            user_group = rdh_settings.COUNTRY_ADMIN_USER_GROUP if rdh_settings.COUNTRY_ADMIN_USER_GROUP else None
            
            if not current_user.is_superuser:            
                if owner_groups.filter(name=rdh_settings.COUNTRY_ADMIN_USER_GROUP).exists():                
                    if not owner_groups.filter(pk__in=current_user_group_ids).exists():
                        result = False
        return result        

    def get(self, request, *args, **kwargs):   
        reg = self.get_region(**kwargs)     
        locations = self.get_location(**kwargs)
        app = self.get_app()
        if not locations:
            return json_response(errors=['Invalid location code'], status=404)
        loc = locations[-1]

        hazard_type = self.get_hazard_type(reg, loc, **kwargs)

        if not hazard_type:
            return json_response(errors=['Invalid hazard type'], status=404)

        (atype_r, atype_e, atypes, aclass,) = self.get_analysis_type(reg, loc, hazard_type, **kwargs)
        
        current_atype = None
        risks = None
        if not atype_r:
            if atype_e:
                if atype_e.analysis_class == aclass:
                    current_atype = atype_e
        else:
            if atype_r.analysis_class == aclass:
                current_atype = atype_r
            if atype_e: 
                if atype_e.analysis_class == aclass:
                    current_atype = atype_e
        
        if not current_atype:
            return json_response(errors=['No analysis type available for location/hazard type'], status=404) 
        
        risks = current_atype.get_risk_analysis_list(id=kwargs['an'])
        if not risks:
            return json_response(errors=['No risk analysis found for given parameters'], status=404)
        risk = risks[0]

        #DETERMINE USER PERMISSIONS
        if not self.is_user_allowed(request, risk):
            return json_response(errors=['Data not available for current user'], status=403) 

        out = {'riskAnalysisData': risk.get_risk_details()}
        
        
        #current parameters                
        parts = rdh_settings.SITEURL.replace('//', '').strip('/').split('/') if rdh_settings.SITEURL else []
        context_url = '/' + parts[len(parts)-1] if len(parts) > 1 else ''
        full_context = {
            'app': app.name,
            'reg': reg.name,
            'adm_level': loc.level,            
            'loc': loc.code,
            'ht': hazard_type.mnemonic,
            'at': current_atype.name,
            'an': risk.id,
            'analysis_class': risk.analysis_type.analysis_class.name,
            'full_url': context_url + '/risks/' + app.name + '/reg/' + reg.name + '/loc/' + loc.code + '/ht/' + hazard_type.mnemonic + '/at/' + current_atype.name + '/an/' + str(risk.id) + '/'
        }
        
        dymlist = risk.dymension_infos.all().distinct()
        if kwargs.get('dym'):
            dimension = dymlist.get(id=kwargs['dym'])
        else:
            dimension = dymlist.filter(riskanalysis_associacion__axis=self.AXIS_X).distinct().get()

        feat_kwargs = self.url_kwargs_to_query_params(**kwargs)
        feat_kwargs['risk_analysis'] = risk.name        
        features = self.get_features(risk, dimension, dymlist, **feat_kwargs)
        
        
        
        out['riskAnalysisData']['data'] = self.reformat_features(risk, dimension, dymlist, features['features'])
        
        out['context'] = self.get_context_url(**kwargs)
        out['wms'] = {'style': None,
                      'viewparams': self.get_viewparams(risk, hazard_type, loc),
                      'baseurl': settings.OGC_SERVER['default']['PUBLIC_LOCATION']}

        out['riskAnalysisData']['unitOfMeasure'] = risk.unit_of_measure
        out['riskAnalysisData']['additionalLayers'] = [(l.id, l.typename, l.title, ) for l in risk.additional_layers.all()]
        out['furtherResources'] = self.get_further_resources(**kwargs)
        #url(r'loc/(?P<loc>[\w\-]+)/ht/(?P<ht>[\w\-]+)/at/(?P<at>[\w\-]+)/an/(?P<an>[\w\-]+)/pdf/$', views.pdf_report, name='pdf_report'),
        out['pdfReport'] = app.url_for('pdf_report', loc.code, hazard_type.mnemonic, current_atype.name, risk.id)
        out['fullContext'] = full_context


        if risk.analysis_type.analysis_class.name == 'event':
            #add fields for managing a layer for events
            out['riskAnalysisData']['eventAreaSelected'] = ''
            out['riskAnalysisData']['eventsLayer'] = {}
            out['riskAnalysisData']['eventsLayer']['layerName'] = '{}_events'.format(out['riskAnalysisData']['layer']['layerName'])
            out['riskAnalysisData']['eventsLayer']['layerStyle'] = {
                'name': 'monochromatic',
                'title': None,
                'url': 'http://localhost:8080/geoserver/rest/styles/monochromatic.sld'
            }
            #out['riskAnalysisData']['eventsLayer']['layerStyle']['url'] = out['riskAnalysisData']['layer']['layerStyle']['url']
            out['riskAnalysisData']['eventsLayer']['layerTitle'] = '{}_events'.format(out['riskAnalysisData']['layer']['layerTitle'])

            # retrieve values for events aggregated by country
            field_list = ['adm_code', 'dim1_value', 'dim2_value', 'value', 'event_id']
            field_list_group = ['adm_code', 'dim1_value', 'dim2_value', 'value']
            feat_kwargs['level'] = loc.level
            features_event_group_country = self.get_features_base('geonode:risk_analysis_event_group', field_list_group, **feat_kwargs)
            features_event_values = self.get_features_base('geonode:risk_analysis_event_details', field_list, **feat_kwargs)        
            values_events = {}
            for f in features_event_values['features']:
                temp = []                
                for l in field_list:
                    temp.append(f['properties'][l])
                values_events[temp[field_list.index('event_id')]] = temp
            
            event_group_country = [[f['properties']['adm_code'], f['properties']['dim1_value'], f['properties']['dim2_value'], f['properties']['value']] for f in features_event_group_country['features']]            
            
            events = Event.objects.filter(hazard_type=hazard_type, region=reg)
            if loc.level == 1:
                events = events.filter(iso2=loc.code)
            elif loc.level >= 2:
                events = events.filter(nuts3__contains=loc.code)
            
            #check if need to filter by date
            if events and 'from' in kwargs and 'to' in kwargs:
                try:
                    date_from = parse(kwargs.get('from'))
                    date_to = parse(kwargs.get('to'))
                    events = events.filter(begin_date__range=(date_from, date_to))
                except ValueError:
                    return json_response(errors=['Invalid date format'], status=400)
            
            events = events.order_by('-begin_date')
            total = events.count()
            
            if events and 'load' not in kwargs and 'from' not in kwargs and total > 50:
                events = events[:50]
            ev_list = []
            data_key = values_events.values()[0][1]
            for event in events:
                e = event.get_event_plain()                
                value_arr = values_events[e['event_id']] if e['event_id'] in values_events else None
                try:                    
                    e[data_key] = float(value_arr[3]) if value_arr is not None else None
                except:
                    e[data_key] = None
                e['data_key'] = data_key                
                ev_list.append(e)
            
            
            out['riskAnalysisData']['data']['event_group_country'] = event_group_country
            out['riskAnalysisData']['data']['total_events'] = total
            #out['riskAnalysisData']['events'] = serializers.serialize("json", events, use_natural_foreign_keys=True, use_natural_primary_keys=True)
            out['riskAnalysisData']['events'] = ev_list            
        
        return json_response(out)

    def get_viewparams(self, risk, htype, loc):
        return 'risk_analysis:{};hazard_type:{};adm_code:{};d1:{{}};d2:{{}}'.format(risk.name, htype.mnemonic, loc.code)


class EventDetailsView(DataExtractionView):
    def get_risk_analysis(self, **kwargs):
        try:
            return RiskAnalysis.objects.get(id=kwargs['an'])
        except RiskAnalysis.DoesNotExist:
            pass

    def get_risk_analysis_group(self, hazard_type, **kwargs):
        ref_ra = self.get_risk_analysis(**kwargs)        
        analysis_types = AnalysisType.objects.filter(analysis_class=ref_ra.analysis_type.analysis_class)
        return RiskAnalysis.objects.filter(hazard_type=hazard_type, analysis_type__in=analysis_types)

    def get_event(self, **kwargs):
        try:
            return Event.objects.get(event_id=kwargs['evt'])
        except Event.DoesNotExist:
            pass

    def get_related_ra(self, hazard_type, dym_values, analysis_type, event):        
        ra = RiskAnalysis.objects.filter(
            hazard_type=hazard_type,
            dymensioninfo_associacion__value__upper__in=dym_values,
            analysis_type=analysis_type,
            show_in_event_details=True)
        if event.event_type:
            ra = ra.filter(tags__icontains=event.event_type)
        return ra 

    def removekey(self, d, key):
        r = dict(d)
        del r[key]
        return r   

    def get_related_analysis_type(self, risk_analysis):
        current_atype_name = risk_analysis.analysis_type.name
        if current_atype_name.startswith('e_'):
            try:
                return AnalysisType.objects.get(name=re.sub(r"^e_", r"r_", current_atype_name))  
            except AnalysisType.DoesNotExist:
                return
        elif current_atype_name.startswith('r_'):
            try:
                return AnalysisType.objects.get(name=re.sub(r"^r_", r"e_", current_atype_name))  
            except AnalysisType.DoesNotExist:
                return
    
    def get(self, request, *args, **kwargs):        
        event = self.get_event(**kwargs)
        #location = self.get_location_exact(event.iso2)
        #retrieve data about nuts2 which are not in AdministrativeDivision models 
        nuts3_adm_divs = AdministrativeDivision.objects.filter(level=2, code__in=event.nuts3.split(';'))
        nuts3_ids = nuts3_adm_divs.values_list('id', flat=True)                   
        nuts2_codes = AdministrativeDivisionMappings.objects.filter(child__pk__in=nuts3_ids).order_by('code').values_list('code', flat=True).distinct()
        nuts3_in_nuts2 = list(AdministrativeDivisionMappings.objects.filter(code__in=nuts2_codes).values_list('child__code', flat=True))
        #locations = self.get_location_range(event.nuts3.split(';') + [event.iso2])
        locations = self.get_location_range(nuts3_in_nuts2 + [event.iso2])
        hazard_type = self.get_hazard_type(event.region, locations[0], **kwargs)
        risk_analysis = self.get_risk_analysis(**kwargs)
        an_group = self.get_risk_analysis_group(hazard_type, **kwargs)        
        data = {}  
        overview = {}      
        if an_group and event:
            
            #administrative data
            administrative_data = {}            
            risk_analysis_mapping = {}
            adm_data_entries = AdministrativeData.objects.all()
            location_adm_data = AdministrativeDivisionDataAssociation.objects.filter(adm__in=locations)            
            
            for adm_data_entry in adm_data_entries:                 
                ra_match = RiskAnalysis.objects.filter(hazard_type=hazard_type, region=risk_analysis.region, analysis_type__name__contains=adm_data_entry.indicator_type).first()
                if ra_match:
                    risk_analysis_mapping[adm_data_entry.name] = ra_match.analysis_type.name
                administrative_data[adm_data_entry.name] = {
                        'unitOfMeasure': adm_data_entry.unit_of_measure,
                        'values': {}
                }
                for location in locations:
                    data_exact = location_adm_data.filter(data=adm_data_entry, adm=location).order_by('-dimension').first()                                        
                    if data_exact:   
                        administrative_data[adm_data_entry.name]['values'][data_exact.adm.code] = data_exact.value

            overview = {                
                'event': event.get_event_plain(),
                'administrativeData': administrative_data,
                'riskAnalysisMapping': risk_analysis_mapping,
                'threshold': 1.5
            }

            for an_event in an_group:                
                adjusted_kwargs = {
                    'loc': event.iso2,                    
                    'ht': kwargs['ht'],
                    'evt': kwargs['evt'],
                    'an': an_event.name
                }            
                feat_kwargs = self.url_kwargs_to_query_params(**adjusted_kwargs)
                features = self.get_features_base('geonode:risk_analysis_event_details', None, **feat_kwargs)                
                dymlist = an_event.dymension_infos.all().distinct()
                dimension = dymlist.filter(riskanalysis_associacion__axis=self.AXIS_X).distinct().get()                
                an_event_values = self.reformat_features(an_event, dimension, dymlist, features['features'], True)  
                data['{}'.format(an_event.analysis_type.name)] = an_event_values
                data['{}'.format(an_event.analysis_type.name)]['riskAnalysis'] = an_event.get_risk_details()

                dym_values = [v[0] for v in an_event_values['values']]
                
                #for every analysis bound to current event, find matching risk analysis (based on analysis type)
                matching_ra = self.get_related_ra(hazard_type, dym_values, self.get_related_analysis_type(an_event), event)                        
                
                #for every match, retrieve sum of values of administrative divisions affected
                for an_risk in matching_ra:
                    adjusted_kwargs['an'] = an_risk.name
                    adjusted_kwargs['loc'] = event.nuts3.replace(';', '__')
                    feat_kwargs = self.url_kwargs_to_query_params(**self.removekey(adjusted_kwargs, 'evt'))
                    features = self.get_features_base('geonode:risk_analysis_grouped_values', None, **feat_kwargs)

                    dymlist = an_risk.dymension_infos.all().distinct()
                    if kwargs.get('dym'):
                        dimension = dymlist.get(id=kwargs['dym'])
                    else:
                        dimension = dymlist.filter(riskanalysis_associacion__axis=self.AXIS_X).distinct().get()                    

                    an_risk_values = self.reformat_features(an_risk, dimension, dymlist, features['features'], True)                

                    merged_values = an_event_values['values'] + an_risk_values['values']
                    data['{}'.format(an_event.analysis_type.name)]['values'] = merged_values # [[str(item).capitalize() for item in row] for row in merged_values]
                   
        return json_response({ 'data': data, 'overview': overview })


class CostBenefitAnalysisView(HazardTypeView):

    def get(self, request, *args, **kwargs):
        pass

class LayersListForm(forms.Form):
    layers = forms.MultipleChoiceField(required=False, choices=())

    def get_layers(self):
        if not self.is_valid():
            return []
        d = self.cleaned_data
        return Layer.objects.filter(id__in=d['layers'])


class RiskLayersView(FormView):
    form_class = LayersListForm

    def get_risk(self):
        rid = self.kwargs['risk_id']
        try:
            return RiskAnalysis.objects.get(id=rid)
        except RiskAnalysis.DoesNotExist:
            pass

    def get_layer_choices(self):
        r = self.get_risk()
        if r.layer is None:
            q = Layer.objects.all().values_list('id', flat=True)
        else:
            q = Layer.objects.exclude(id=r.layer.id).values_list('id', flat=True)
        return [(str(val), str(val),) for val in q]

    def get_form(self, form_class=None):
        f = super(RiskLayersView, self).get_form(form_class)
        choices = self.get_layer_choices()
        f.fields['layers'].choices = choices
        return f


    def form_invalid(self, form):
        err = form.errors
        return json_response({'errors': err}, status=400)

    def form_valid(self, form):
        rid = self.kwargs['risk_id']
        risk = self.get_risk()
        if risk is None:
            return json_response({'errors': ['Invalid risk id']}, status=404)

        data = form.cleaned_data

        risk.additional_layers.clear()
        layers = form.get_layers()
        risk.additional_layers.add(*layers)
        risk.save()
        return self.get()


    def get(self, *args, **kwargs):
        rid = self.kwargs['risk_id']
        risk = self.get_risk()
        if risk is None:
            return json_response({'errors': ['Invalid risk id']}, status=404)
        out = {}
        out['success'] = True
        out['data'] = {'layers': list(risk.additional_layers.all().values_list('typename', flat=True))}
        return json_response(out)


class CleaningFileResponse(FileResponse):
    def __init__(self, *args, **kwargs):

        on_close = kwargs.pop('on_close', None)
        super(CleaningFileResponse, self).__init__(*args, **kwargs)
        self._on_close = on_close

    def close(self):
        print('closing', self)
        if callable(self._on_close):
            self._on_close()
        super(CleaningFileResponse, self).close()

class PDFUploadsForm(forms.Form):
    map = forms.ImageField(required=True)
    chart_0 = forms.ImageField(required=True)
    chart_1 = forms.ImageField(required=False)
    chart_2 = forms.ImageField(required=False)
    chart_3 = forms.ImageField(required=False)
    legend = forms.ImageField(required=False)
    permalink = forms.URLField(required=False)
    dims = ValuesListField(required=True)
    dimsVal = ValuesListField(required=True)


class PDFReportView(ContextAware, FormView):
    form_class = PDFUploadsForm
    CONTEXT_KEYS = ContextAware.CONTEXT_KEYS + ['loc']
    TEMPLATE_NAME = 'risks/pdf/{}.{}.html'

    PDF_PARTS = ['cover', 'report', 'footer']

    def get_client_url(self, app, **kwargs):

        #http://localhost:8000/risks/data_extraction/?init={"href":"/risks/data_extraction/loc/AF/","geomHref":"/risks/data_extraction/geom/AF/","gc":"ht/EQ/","ac":"ht/EQ/at/impact/an/6/","d":{"dim1":0,"dim2":1,"dim1Idx":0,"dim2Idx":0},"s":{}}
        out = {'href': app.url_for('location', kwargs['loc']),
               'geomHref': app.url_for('geometry', kwargs['loc']),
               'ac': self.get_context_url(**kwargs),
               'gc': self.get_context_url(ht=kwargs['ht']),}
               #'s': {},
               #'d': {}}
        return json.dumps(out)

    def get_context_data(self, *args, **kwargs):
        ctx = super(PDFReportView, self).get_context_data(*args, **kwargs)

        r = self.request
        randomizer = self.request.GET.get('r') or ''
        ctx['app'] = app = self.get_app()
        ctx['kwargs'] = k = self.kwargs
        report_uri = app.url_for('index')
        client_kwargs = k.copy()
        client_kwargs.pop('app', None)
        context = self.get_context_url(_full=True, **k)
        ctx['context'] = {'url': context,
                          'parts': self.get_further_resources_inputs(**k)}
        fr_map = self.get_further_resources(inputs=ctx['context']['parts'], **k)
        further_resources = []
        for fr_key, fr_list in fr_map.items():
            for fr_item in fr_list:
                # we could do it with set(), but we want to preserve order
                if fr_item in further_resources:
                    continue
                further_resources.append(fr_item)
        ctx['context']['further_resources'] = further_resources

        ctx['risk_analysis'] = risk_analysis = RiskAnalysis.objects.get(id=k['an'])

        def p(val):
            # for test we need full fs path
            if settings.TEST:
                return default_storage.path(val)
            # otherwise, we need nice absolute url
            _path = default_storage.url(val)
            return r.build_absolute_uri(_path)
        ctx['paths'] = {'map': p(os.path.join(context, 'map_{}.png'.format(randomizer) if randomizer else 'map.png')),
                        'charts': [],
                        'legend': p(os.path.join(context, 'legend_{}.png'.format(randomizer) if randomizer else 'legend.png'))}
        
        for cidx in range(0, 4):
            chart_path = os.path.join(context, 'chart_{}_{}.png'.format(cidx, randomizer) if randomizer else 'chart_{}.png'.format(cidx))
            if not os.path.exists(default_storage.path(chart_path)):
                continue
            chart_f = p(chart_path)
            ctx['paths']['charts'].append(chart_f)

        ctx['resources'] = {}
        ctx['resources']['permalink'] =  '{}?init={}'.format(r.build_absolute_uri(report_uri), self.get_client_url(app, **client_kwargs))

        for resname in ('permalink', 'dims', 'dimsVal',):
            _fname = os.path.join(context, '{}_{}.txt'.format(resname, randomizer) if randomizer else '{}.txt'.format(resname))
            fname = default_storage.path(_fname)
            if os.path.exists(fname):
                with open(fname, 'rt') as f:
                    data = json.loads(f.read())
                    ctx['resources'][resname] = data

        ctx['dimensions'] = self.get_dimensions(risk_analysis, ctx['resources'])
        return ctx

    def get_dimensions(self, risk_analysis, selected):
        dims = selected['dims']
        dimsVal = selected['dimsVal']
        headers = []
        _values = []

        def make_selected(r, sel):
            r.selected = r.value == sel
            return r

        for didx, dname in enumerate(dims):
            dselected = dimsVal[didx]
            di = risk_analysis.dymensioninfo_set.filter(name=dname).distinct().first()
            headers.append(di)
            rows = [make_selected(r, dselected) for r in risk_analysis.dymensioninfo_associacion.filter(dymensioninfo=di)]
            _values.append(rows)
            
        values = zip(*_values)
        return {'headers': headers,
                'values': values}

    def get_document_urls(self, app, randomizer):
        out = []
        r = self.request
        k = self.kwargs.copy()
        for part in self.PDF_PARTS:
            if part != 'report':
                continue
            k['pdf_part'] = part
            out.append(r.build_absolute_uri('{}?r={}'.format(app.url_for('pdf_report_part', **k), randomizer)))

        return out

    def get_template_names(self):
        app = self.get_app()
        pdf_part = self.kwargs['pdf_part']
        out = [self.TEMPLATE_NAME.format(app.name, pdf_part)]
        return out

    def form_invalid(self, form):
        out = {'succes': False, 'errors': form.errors}
        log.error("Cannot generate pdf: %s: %s", self.request.build_absolute_uri(), form.errors)
        return json_response(out, status=400)

    def form_valid(self, form):
        ctx = self.get_context_url(_full=True, **self.kwargs)

        r = self.request
        out = {'success': True}
        app = self.get_app()
        config = {}

        randomizer = get_random_string(7)
        cleanup_paths = []
        for k, v in form.cleaned_data.iteritems():
            if v is None:
                continue
            if not isinstance(v, File):
                target_path = os.path.join(ctx, '{}_{}.txt'.format(k, randomizer))
                v = ContentFile(json.dumps(v))
                target_path = default_storage.save(target_path, v)
                cleanup_paths.append(default_storage.path(target_path))
                
            else:
                basename, ext = os.path.splitext(v.name)
                target_path = os.path.join(ctx, '{}_{}.png'.format(k, randomizer))
                target_path = default_storage.save(target_path, v)
                cleanup_paths.append(default_storage.path(target_path))

            if settings.TEST:
                full_path = default_storage.path(target_path)
                target_path = full_path
            else:
                target_path = default_storage.url(target_path)

            config[k] = target_path

        pdf_path = default_storage.path(os.path.join(ctx, 'report_{}.pdf'.format(randomizer)))
        cleanup_paths.append(pdf_path)
        config['pdf'] = pdf_path
        config['urls'] = self.get_document_urls(app, randomizer)

        pdf = generate_pdf(**config)
        out['pdf'] = pdf

        def cleanup():
            self.cleanup(cleanup_paths)

        with open(pdf, 'rb') as fd:
            data = fd.read()


        resp = HttpResponse(data, content_type='application/pdf')
        resp['Content-Disposition'] = 'attachment; filename="report.pdf"'
        cleanup()
        return resp

        #return CleaningFileResponse(f, on_close=cleanup)

    def cleanup(self, paths):
        for path in paths:
            if os.path.exists(path):
                try:
                    os.unlink(path)
                except OSError, err:
                    print('error when removing', path, err)

    def render_report_markup(self, ctx, request, *args, **kwargs):

        html_path = os.path.join(ctx, 'template.html')
        html_path_absolute = default_storage.path(html_path)

        pdf_ctx = self.get_context_data(*args, **kwargs)
        html_template = self.get_template_names()[0]
        tmpl = render_to_string(html_template, pdf_ctx, request=self.request)
        default_storage.save(html_path, ContentFile(tmpl))

        return html_path_absolute

class AuthorizationView(ContextAware, View):
    def post(self, request, *args, **kwargs):        
        if 'app' in kwargs:            
            region_name = request.POST.get("app[regionName]", "")
            owner_groups = None
            try:
                region = Region.objects.get(name=region_name)
                owner_groups = region.owner.groups.all() if region.owner else None
            except Region.DoesNotExist:
                return json_response(errors=['No data available for selected country'], status=404)

            current_user = request.user
            current_user_group_ids = current_user.groups.all().values_list('id', flat=True)
            user_group = rdh_settings.COUNTRY_ADMIN_USER_GROUP if rdh_settings.COUNTRY_ADMIN_USER_GROUP else None

            if not current_user.is_superuser:  
                if owner_groups:
                    if owner_groups.filter(name=rdh_settings.COUNTRY_ADMIN_USER_GROUP).exists():                
                        if not owner_groups.filter(pk__in=current_user_group_ids).exists():
                            return json_response(errors=['You are not allowed to access the requested resources'], status=403)
                        
        return json_response({'success': True})

class TestView(ContextAware, View):
    def get(self, request, *args, **kwargs):
        app_array = []
        for app in apps.get_app_configs():
            app_array.append({'appname': '{}'.format(app.verbose_name)})

        return json_response({'apps': app_array})                    

    def post(self, request, *args, **kwargs):
        app_array = []
        for app in apps.get_app_configs():
            app_array.append({'appname': '{}'.format(app.verbose_name)})

        return json_response({'apps': app_array})                            

CACHE_TTL = 120
location_view = cache_page(CACHE_TTL)(LocationView.as_view()) 
hazard_type_view = cache_page(CACHE_TTL)(HazardTypeView.as_view())
analysis_type_view = cache_page(CACHE_TTL)(HazardTypeView.as_view())
data_extraction = cache_page(CACHE_TTL)(DataExtractionView.as_view())
event_view = cache_page(CACHE_TTL)(EventView.as_view())
event_details_view = cache_page(CACHE_TTL)(EventDetailsView.as_view())
adm_lookup_view = cache_page(CACHE_TTL)(AdmLookupView.as_view())
auth_view = cache_page(CACHE_TTL)(AuthorizationView.as_view())
apps_view = cache_page(CACHE_TTL)(TestView.as_view())

risk_layers = RiskLayersView.as_view()
pdf_report = PDFReportView.as_view()
