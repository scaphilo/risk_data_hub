#!/usr/bin/env python
# -*- coding: utf-8 -*-

import StringIO
import traceback

from celery.task import task
from django.conf import settings
from django.core.mail import send_mail
from django.core.management import call_command

from django.db import IntegrityError, transaction

from risks.models import RiskAnalysis, HazardSet, HazardType

def create_risk_analysis(input_file, file_ini):
    _create_risk_analysis.apply_async(args=(input_file, file_ini))


@task(name='risks.tasks.create_risk_analysis')
def _create_risk_analysis(input_file, file_ini):
    out = StringIO.StringIO()
    risk = None
    try:
        call_command('createriskanalysis',
                     descriptor_file=str(input_file).strip(), stdout=out)
        value = out.getvalue()

        risk = RiskAnalysis.objects.get(name=str(value).strip())
        print('risk found: {}'.format(risk.name))
        try:
            with transaction.atomic():
                risk.descriptor_file = file_ini
                risk.save()
        except IntegrityError:
            #new_risk = RiskAnalysis()
            #new_risk = risk
            #new_risk.save()
	    pass
    except Exception, e:
        value = None
        if risk is not None:
            risk.set_error()
        error_message = "Sorry, the input file is not valid: {}".format(e)
        raise ValueError(error_message)


def import_risk_data(input_file, risk_app, risk_analysis, region, final_name):
    risk_analysis.set_queued()
    _import_risk_data.apply_async(args=(input_file, risk_app.name, risk_analysis.name, region.name, final_name,))

@task(name='risks.tasks.import_risk_data')
def _import_risk_data(input_file, risk_app_name, risk_analysis_name, region_name, final_name):
        out = StringIO.StringIO()
        risk = None
        try:
            risk = RiskAnalysis.objects.get(name=risk_analysis_name)
            risk.set_processing()
            # value = out.getvalue()
            call_command('importriskdata',
                         commit=False,
                         risk_app=risk_app_name,
                         region=region_name,
                         excel_file=input_file,
                         risk_analysis=risk_analysis_name,
                         stdout=out)
            risk.refresh_from_db()
            risk.data_file = final_name
            risk.save()
            risk.set_ready()
        except Exception, e:
            error_message = "Sorry, the input file is not valid: {}".format(e)
            if risk is not None:
                risk.save()
                risk.set_error()
            raise ValueError(error_message)

def import_risk_metadata(input_file, risk_app, risk_analysis, region, final_name):
    risk_analysis.set_queued()
    _import_risk_metadata.apply_async(args=(input_file, risk_app.name, risk_analysis.name, region.name, final_name,))


@task(name='risks.tasks.import_risk_metadata')
def _import_risk_metadata(input_file, risk_app_name, risk_analysis_name, region_name, final_name):
        out = StringIO.StringIO()
        risk = None
        try:
            risk = RiskAnalysis.objects.get(name=risk_analysis_name)
            risk.set_processing()
            call_command('importriskmetadata',
                         commit=False,
                         risk_app=risk_app_name,
                         region=region_name,
                         excel_file=input_file,
                         risk_analysis=risk_analysis_name,
                         stdout=out)
            # value = out.getvalue()
            risk.refresh_from_db()
            risk.metadata_file = final_name
            hazardsets = HazardSet.objects.filter(riskanalysis__name=risk_analysis_name,
                                                  country__name=region_name)
            if len(hazardsets) > 0:
                hazardset = hazardsets[0]
                risk.hazardset = hazardset

            risk.save()
            risk.set_ready()
        except Exception, e:
            error_message = "Sorry, the input file is not valid: {}".format(e)
            if risk is not None:
                risk.set_error()
            raise ValueError(error_message)


'''def import_event_data(input_file, risk_app, hazard_type, region, final_name):
    hazard_type.set_queued()
    _import_event_data.apply_async(args=(input_file, risk_app.name, hazard_type.mnemonic, region.name, final_name,))

@task(name='risks.tasks.import_event_data')
def _import_event_data(input_file, risk_app_name, hazard_type_name, region_name, final_name):
        out = StringIO.StringIO()
        hazard = None
        try:
            hazard = HazardType.objects.get(mnemonic=hazard_type_name)
            hazard.set_processing()
            call_command('importriskevents',
                         commit=False,
                         risk_app=risk_app_name,
                         region=region_name,
                         excel_file=input_file,
                         hazard_type=hazard_type_name,
                         stdout=out)
            hazard.refresh_from_db()
            hazard.data_file = final_name
            hazard.save()
            hazard.set_ready()
        except Exception, e:
            error_message = "Sorry, the input file is not valid: {}".format(e)
            if hazard is not None:
                hazard.save()
                hazard.set_error()
            raise ValueError(error_message)'''

def import_event_data(input_file, risk_app, region, final_name):    
    _import_event_data.apply_async(args=(input_file, risk_app.name, region.name, final_name,))

@task(name='risks.tasks.import_event_data')
def _import_event_data(input_file, risk_app_name, region_name, final_name):
        out = StringIO.StringIO()        
        try:            
            call_command('importriskevents',
                         commit=False,
                         risk_app=risk_app_name,
                         region=region_name,
                         excel_file=input_file,                         
                         stdout=out)            
        except Exception, e:
            error_message = "Sorry, the input file is not valid: {}".format(e)            
            raise ValueError(error_message)

def import_event_attributes(input_file, risk_app, risk_analysis, region, allow_null_values, final_name):    
    _import_event_attributes.apply_async(args=(input_file, risk_app.name, risk_analysis.name, region.name, allow_null_values, final_name,))

@task(name='risks.tasks.import_event_attributes')
def _import_event_attributes(input_file, risk_app_name, risk_analysis_name, region_name, allow_null_values, final_name):
        out = StringIO.StringIO()        
        try:            
            call_command('import_event_attributes',
                         commit=False,
                         risk_app=risk_app_name,                         
                         region=region_name,
                         allow_null_values=allow_null_values,
                         excel_file=input_file,
                         risk_analysis=risk_analysis_name,                        
                         stdout=out)            
        except Exception, e:
            error_message = "Sorry, the input file is not valid: {}".format(e)            
            raise ValueError(error_message)

