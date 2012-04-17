"""GeoNode SDK for managing GeoNode layers and users
"""

# Standard Modules
import logging
import re
import uuid
import sys
import os
import datetime
import traceback
import inspect
import string
import urllib2
from lxml import etree
import glob
from itertools import cycle, izip
#from xml.etree import ElementTree as etree

# Django functionality
from django.db import transaction
from django.utils.translation import ugettext as _
from django.contrib.auth.models import User
from django.template.defaultfilters import slugify
from django.conf import settings

# Geonode functionality
from geonode.maps.models import Map, Layer, MapLayer
from geonode.maps.models import Contact, ContactRole, Role, get_csw
from geonode.maps.gs_helpers import fixup_style, cascading_delete, get_sld_for, delete_from_postgis

# Geoserver functionality
import geoserver
from geoserver.catalog import FailedRequestError
from geoserver.resource import FeatureType, Coverage

# CSW functionality
from geonode.catalogue.catalogue import gen_iso_xml, gen_anytext
from owslib.csw import CswRecord
from owslib.iso import MD_Metadata
from owslib.fgdc import Metadata

logger = logging.getLogger('geonode.maps.utils')
_separator = '\n' + ('-' * 100) + '\n'
import math

class GeoNodeException(Exception):
    """Base class for exceptions in this module."""
    pass


def layer_type(filename):
    """Finds out if a filename is a Feature or a Vector
       returns a gsconfig resource_type string
       that can be either 'featureType' or 'coverage'
    """
    base_name, extension = os.path.splitext(filename)
    if extension.lower() in ['.shp']:
        return FeatureType.resource_type
    elif extension.lower() in ['.tif', '.tiff', '.geotiff', '.geotif']:
        return Coverage.resource_type
    else:
        msg = ('Saving of extension [%s] is not implemented' % extension)
        raise GeoNodeException(msg)


def get_files(filename):
    """Converts the data to Shapefiles or Geotiffs and returns
       a dictionary with all the required files
    """
    files = {'base': filename}

    base_name, extension = os.path.splitext(filename)

    if extension.lower() == '.shp':
        required_extensions = dict(
            shp='.[sS][hH][pP]', dbf='.[dD][bB][fF]', shx='.[sS][hH][xX]')
        for ext, pattern in required_extensions.iteritems():
            matches = glob.glob(base_name + pattern)
            if len(matches) == 0:
                msg = ('Expected helper file %s does not exist; a Shapefile '
                       'requires helper files with the following extensions: '
                       '%s') % (base_name + "." + ext,
                                required_extensions.keys())
                raise GeoNodeException(msg)
            elif len(matches) > 1:
                msg = ('Multiple helper files for %s exist; they need to be '
                       'distinct by spelling and not just case.') % filename
                raise GeoNodeException(msg)
            else:
                files[ext] = matches[0]

        matches = glob.glob(base_name + ".[pP][rR][jJ]")
        if len(matches) == 1:
            files['prj'] = matches[0]
        elif len(matches) > 1:
            msg = ('Multiple helper files for %s exist; they need to be '
                   'distinct by spelling and not just case.') % filename
            raise GeoNodeException(msg)

    matches = glob.glob(base_name + ".[sS][lL][dD]")
    if len(matches) == 1:
        files['sld'] = matches[0]
    elif len(matches) > 1:
        msg = ('Multiple style files for %s exist; they need to be '
               'distinct by spelling and not just case.') % filename
        raise GeoNodeException(msg)

    matches = glob.glob(base_name + ".[xX][mM][lL]")

    if len(matches) == 1:
        files['xml'] = matches[0]
    elif len(matches) > 1:
        msg = ('Multiple XML files for %s exist; they need to be '
               'distinct by spelling and not just case.') % filename
        raise GeoNodeException(msg)

    return files


def get_valid_name(layer_name):
    """Create a brand new name
    """
    xml_unsafe = re.compile(r"(^[^a-zA-Z\._]+)|([^a-zA-Z\._0-9]+)")
    name = xml_unsafe.sub("_", layer_name)
    proposed_name = name
    count = 1
    while Layer.objects.filter(name=proposed_name).count() > 0:
        proposed_name = "%s_%d" % (name, count)
        count = count + 1
        logger.info('Requested name already used; adjusting name '
                    '[%s] => [%s]', layer_name, proposed_name)
    else:
        logger.info("Using name as requested")

    return proposed_name


## TODO: Remove default arguments here, they are never used.
def get_valid_layer_name(layer=None, overwrite=False):
    """Checks if the layer is a string and fetches it from the database.
    """
    # The first thing we do is get the layer name string
    if isinstance(layer, Layer):
        layer_name = layer.name
    elif isinstance(layer, basestring):
        layer_name = layer
    else:
        msg = ('You must pass either a filename or a GeoNode layer object')
        raise GeoNodeException(msg)

    if overwrite:
        #FIXME: What happens if there is a store in GeoServer with that name
        # that is not registered in GeoNode?
        return layer_name
    else:
        return get_valid_name(layer_name)


def cleanup(name, uuid):
   """Deletes GeoServer and GeoNetwork records for a given name.

      Useful to clean the mess when something goes terribly wrong.
      It also verifies if the Django record existed, in which case
      it performs no action.
   """
   try:
       Layer.objects.get(name=name)
   except Layer.DoesNotExist, e:
       pass
   else:
       msg = ('Not doing any cleanup because the layer %s exists in the '
              'Django db.' % name)
       raise GeoNodeException(msg)

   cat = Layer.objects.gs_catalog
   gs_store = None
   gs_layer = None
   gs_resource = None
   # FIXME: Could this lead to someone deleting for example a postgis db
   # with the same name of the uploaded file?.
   try:
       gs_store = cat.get_store(name)
       if gs_store is not None:
           gs_layer = cat.get_layer(name)
           if gs_layer is not None:
               gs_resource = gs_layer.resource
       else:
           gs_layer = None
           gs_resource = None
   except FailedRequestError, e:
       msg = ('Couldn\'t connect to GeoServer while cleaning up layer '
              '[%s] !!', str(e))
       logger.error(msg)

   if gs_layer is not None:
       try:
           cat.delete(gs_layer)
       except:
           logger.exception("Couldn't delete GeoServer layer during cleanup()")
   if gs_resource is not None:
       try:
           cat.delete(gs_resource)
       except:
           msg = 'Couldn\'t delete GeoServer resource during cleanup()'
           logger.exception(msg)
   if gs_store is not None:
       try:
           cat.delete(gs_store)
       except:
           logger.exception("Couldn't delete GeoServer store during cleanup()")

   cat = Layer.objects.catalogue
   csw_record = cat.get_by_uuid(uuid)
   if csw_record is not None:
       logger.warning('Deleting dangling GeoNetwork record for [%s] '
                      '(no Django record to match)', name)
       try:
           # this is a bit hacky, delete_layer expects an instance of the layer
           # model but it just passes it to a Django template so a dict works
           # too.
           cat.delete_layer({ "uuid": uuid }) 
       except:
           logger.exception('Couldn\'t delete GeoNetwork record '
                            'during cleanup()')

   logger.warning('Finished cleanup after failed GeoNetwork/Django '
                  'import for layer: %s', name)


def save(layer, base_file, user, overwrite = True, title=None,
         abstract=None, permissions=None, keywords = []):
    """Upload layer data to Geoserver and registers it with Geonode.

       If specified, the layer given is overwritten, otherwise a new layer
       is created.
    """
    logger.info(_separator)
    logger.info('Uploading layer: [%s], base filename: [%s]', layer, base_file)

    # Step 0. Verify the file exists
    logger.info('>>> Step 0. Verify if the file %s exists so we can create '
                'the layer [%s]' % (base_file, layer))
    if not os.path.exists(base_file):
        msg = ('Could not open %s to save %s. Make sure you are using a '
               'valid file' %(base_file, layer))
        logger.warn(msg)
        raise GeoNodeException(msg)

    # Step 1. Figure out a name for the new layer, the one passed might not
    # be valid or being used.
    logger.info('>>> Step 1. Figure out a name for %s', layer)
    name = get_valid_layer_name(layer, overwrite)

    # Step 2. Check that it is uploading to the same resource type as
    # the existing resource
    logger.info('>>> Step 2. Make sure we are not trying to overwrite a '
                'existing resource named [%s] with the wrong type', name)
    the_layer_type = layer_type(base_file)

    # Get a short handle to the gsconfig geoserver catalog
    cat = Layer.objects.gs_catalog

    # Check if the store exists in geoserver
    try:
        store = cat.get_store(name)
    except geoserver.catalog.FailedRequestError, e:
        # There is no store, ergo the road is clear
        pass
    else:
        # If we get a store, we do the following:
        resources = store.get_resources()
        # Is it empty?
        if len(resources) == 0:
            # What should we do about that empty store?
            if overwrite:
                # We can just delete it and recreate it later.
                store.delete()
            else:
                msg = ('The layer exists and the overwrite parameter is '
                       '%s' % overwrite)
                raise GeoNodeException(msg)
        else:
            # If our resource is already configured in the store it needs
            # to have the right resource type
            for resource in resources:
                if resource.name == name:
                    msg = 'Name already in use and overwrite is False'
                    assert overwrite, msg
                    existing_type = resource.resource_type
                    if existing_type != the_layer_type:
                        msg =  ('Type of uploaded file %s (%s) does not match '
                                'type of existing resource type '
                                '%s' % (layer_name,
                                        the_layer_type,
                                        existing_type))
                        logger.info(msg)
                        raise GeoNodeException(msg)

    # Step 3. Identify whether it is vector or raster and which extra files
    # are needed.
    logger.info('>>> Step 3. Identifying if [%s] is vector or raster and '
                'gathering extra files', name)
    if the_layer_type == FeatureType.resource_type:
        logger.debug('Uploading vector layer: [%s]', base_file)
        if settings.DB_DATASTORE:
            create_store_and_resource = _create_db_featurestore
        else:
            def create_store_and_resource(name, data, overwrite):
                ft = cat.create_featurestore(name, data, overwrite=overwrite)
                return cat.get_store(name), cat.get_resource(name)
    elif the_layer_type == Coverage.resource_type:
        logger.debug("Uploading raster layer: [%s]", base_file)
        def create_store_and_resource(name, data, overwrite):
            cat.create_coveragestore(name, data, overwrite=overwrite)
            return cat.get_store(name), cat.get_resource(name)
    else:
        msg = ('The layer type for name %s is %s. It should be '
               '%s or %s,' % (layer_name,
                              the_layer_type,
                              FeatureType.resource_type,
                              Coverage.resource_type))
        logger.warn(msg)
        raise GeoNodeException(msg)

    # Step 4. Create the store in GeoServer
    logger.info('>>> Step 4. Starting upload of [%s] to GeoServer...', name)

    # Get the helper files if they exist
    files = get_files(base_file)

    data = files

    #FIXME: DONT DO THIS
    #-------------------
    if 'shp' not in files:
        main_file = files['base']
        data = main_file
    # ------------------

    try:
        store, gs_resource = create_store_and_resource(name, data, overwrite=overwrite)
    except geoserver.catalog.UploadError, e:
        msg = ('Could not save the layer %s, there was an upload '
               'error: %s' % (name, str(e)))
        logger.warn(msg)
        e.args = (msg,)
        raise
    except geoserver.catalog.ConflictingDataError, e:
        # A datastore of this name already exists
        msg = ('GeoServer reported a conflict creating a store with name %s: '
               '"%s". This should never happen because a brand new name '
               'should have been generated. But since it happened, '
               'try renaming the file or deleting the store in '
               'GeoServer.'  % (name, str(e)))
        logger.warn(msg)
        e.args = (msg,)
        raise
    else:
        logger.debug('Finished upload of [%s] to GeoServer without '
                     'errors.', name)


    # Step 5. Create the resource in GeoServer
    logger.info('>>> Step 5. Generating the metadata for [%s] after '
                'successful import to GeoSever', name)

    # Verify the resource was created
    if gs_resource is not None:
        assert gs_resource.name == name
    else:
        msg = ('GeoServer returne resource as None for layer %s.'
               'What does that mean? ' % name)
        logger.warn(msg)
        raise GeoNodeException(msg)

    # Step 6. Make sure our data always has a valid projection
    # FIXME: Put this in gsconfig.py
    logger.info('>>> Step 6. Making sure [%s] has a valid projection' % name)
    if gs_resource.latlon_bbox is None:
        box = gs_resource.native_bbox[:4]
        minx, maxx, miny, maxy = [float(a) for a in box]
        if -180 <= minx <= 180 and -180 <= maxx <= 180 and \
           -90  <= miny <= 90  and -90  <= maxy <= 90:
            logger.warn('GeoServer failed to detect the projection for layer '
                        '[%s]. Guessing EPSG:4326', name)
            # If GeoServer couldn't figure out the projection, we just
            # assume it's lat/lon to avoid a bad GeoServer configuration

            gs_resource.latlon_bbox = gs_resource.native_bbox
            gs_resource.projection = "EPSG:4326"
            cat.save(gs_resource)
        else:
            msg = ('GeoServer failed to detect the projection for layer '
                   '[%s]. It doesn\'t look like EPSG:4326, so backing out '
                   'the layer.')
            logger.warn(msg, name)
            cascading_delete(cat, gs_resource)
            raise GeoNodeException(msg % name)

    # Step 7. Create the style and assign it to the created resource
    # FIXME: Put this in gsconfig.py
    logger.info('>>> Step 7. Creating style for [%s]' % name)
    publishing = cat.get_layer(name)

    if 'sld' in files:
        f = open(files['sld'], 'r')
        sld = f.read()
        f.close()
    else:
        sld = get_sld_for(publishing)

    if sld is not None:
        try:
            cat.create_style(name, sld)
        except geoserver.catalog.ConflictingDataError, e:
            msg = ('There was already a style named %s in GeoServer, '
                   'cannot overwrite: "%s"' % (name, str(e)))
            style = cat.get_style(name)
            logger.warn(msg)
            e.args = (msg,)

        #FIXME: Should we use the fully qualified typename?
        publishing.default_style = cat.get_style(name)
        cat.save(publishing)

    # Step 10. Create the Django record for the layer
    logger.info('>>> Step 10. Creating Django record for [%s]', name)
    # FIXME: Do this inside the layer object
    typename = gs_resource.store.workspace.name + ':' + gs_resource.name
    layer_uuid = str(uuid.uuid1())
    defaults = dict(store=gs_resource.store.name,
                    storeType=gs_resource.store.resource_type,
                    typename=typename,
                    title=title or gs_resource.title,
                    uuid=layer_uuid,
                    abstract=abstract or gs_resource.abstract or '',
                    owner=user)
    saved_layer, created = Layer.objects.get_or_create(name=gs_resource.name,
                                                       workspace=gs_resource.store.workspace.name,
                                                       defaults=defaults)

    if created:
        saved_layer.set_default_permissions()
        saved_layer.keywords.add(*keywords)

    # Step 9. Create the points of contact records for the layer
    # A user without a profile might be uploading this
    logger.info('>>> Step 9. Creating points of contact records for '
                '[%s]', name)
    poc_contact, __ = Contact.objects.get_or_create(user=user,
                                           defaults={"name": user.username })
    author_contact, __ = Contact.objects.get_or_create(user=user,
                                           defaults={"name": user.username })

    logger.debug('Creating poc and author records for %s', poc_contact)

    saved_layer.poc = poc_contact
    saved_layer.metadata_author = author_contact

    logger.info('>>> Step XML. If an XML metadata document was passed, process it')
    # Step XML.  If an XML metadata document is uploaded,
    # parse the XML metadata and update uuid and URLs as per the content model

    if 'xml' in files:
        md_xml, md_title, md_abstract = update_metadata(layer_uuid, open(files['xml']).read(), saved_layer)
        Layer.objects.filter(uuid=layer_uuid).update(
            metadata_xml=md_xml,
            metadata_uploaded=True,
            title=md_title,
            abstract=md_abstract,
        )
        saved_layer.metadata_xml = md_xml
        saved_layer.title = md_title
        saved_layer.abstract = md_abstract
        saved_layer.metadata_uploaded = True

        vals = set_metadata(md_xml, saved_layer)
        Layer.objects.filter(uuid=layer_uuid).update(**vals)

    # add to CSW catalogue
    saved_layer.save_to_catalogue()

    xml_doc = gen_iso_xml(saved_layer)

    # save XML doc
    if 'xml' not in files:  # force the saving of an ISO document
        Layer.objects.filter(uuid=layer_uuid).update(metadata_xml=xml_doc, csw_anytext=gen_anytext(xml_doc))

    # Step 11. Set default permissions on the newly created layer
    # FIXME: Do this as part of the post_save hook
    logger.info('>>> Step 11. Setting default permissions for [%s]', name)
    if permissions is not None:
        from geonode.maps.views import set_layer_permissions
        set_layer_permissions(saved_layer, permissions)

    # Step 12. Verify the layer was saved correctly and clean up if needed
    logger.info('>>> Step 12. Verifying the layer [%s] was created '
                'correctly' % name)

    # Verify the object was saved to the Django database
    try:
        Layer.objects.get(name=name)
    except Layer.DoesNotExist, e:
        msg = ('There was a problem saving the layer %s to %s/Django. Error is: %s' % (settings.CSW['type'], layer, str(e)))
        logger.exception(msg)
        logger.debug('Attempting to clean up after failed save for layer '
                     '[%s]', name)
        # Since the layer creation was not successful, we need to clean up
        cleanup(name, layer_uuid)
        raise GeoNodeException(msg)

    # Verify it is correctly linked to GeoServer and the catalogue
    try:
        #FIXME: Implement a verify method that makes sure it was saved in both the catalogue and GeoServer
        saved_layer.verify()
    except NotImplementedError, e:
        logger.exception('>>> FIXME: Please, if you can write python code, '
                         'implement "verify()" '
                         'method in geonode.maps.models.Layer')
    except GeoNodeException, e:
        msg = ('The layer [%s] was not correctly saved to %s/GeoServer. Error is: %s' % (settings.CSW['type'], layer, str(e)))
        logger.exception(msg)
        e.args = (msg,)
        # Deleting the layer
        saved_layer.delete()
        raise

    # Return the created layer object
    return saved_layer


def get_default_user():
    """Create a default user
    """
    try:
        return User.objects.get(is_superuser=True)
    except User.DoesNotExist:
        raise GeoNodeException('You must have an admin account configured '
                               'before importing data. '
                               'Try: django-admin.py createsuperuser')
    except User.MultipleObjectsReturned:
        raise GeoNodeException('You have multiple admin accounts, '
                               'please specify which I should use.')

def get_valid_user(user=None):
    """Gets the default user or creates it if it does not exist
    """
    if user is None:
        theuser = get_default_user()
    elif isinstance(user, basestring):
        theuser = User.objects.get(username=user)
    elif user.is_anonymous():
        raise GeoNodeException('The user uploading files must not '
                               'be anonymous')
    else:
        theuser = user

    #FIXME: Pass a user in the unit tests that is not yet saved ;)
    assert isinstance(theuser, User)

    return theuser


def check_geonode_is_up():
    """Verifies all of geonetwork, geoserver and the django server are running,
       this is needed to be able to upload.
    """
    try:
        Layer.objects.gs_catalog.get_workspaces()
    except:
        # Cannot connect to GeoNode
        from django.conf import settings

        msg = ('Cannot connect to the GeoServer at %s\nPlease make sure you '
               'have started GeoNode.' % settings.GEOSERVER_BASE_URL)
        raise GeoNodeException(msg)

    try:
        Layer.objects.csw_catalogue.login()
    except:
        from django.conf import settings
        msg = ("Cannot connect to the Catalogue at %s\n"
                "Please make sure you have started the CSW." % settings.CSW['url'])
        raise GeoNodeException(msg)

def file_upload(filename, user=None, title=None, overwrite=True, keywords=[]):
    """Saves a layer in GeoNode asking as little information as possible.
       Only filename is required, user and title are optional.
    """

    # Do not do attemt to do anything unless geonode is running
    check_geonode_is_up()

    # Get a valid user
    theuser = get_valid_user(user)

    # Set a default title that looks nice ...
    if title is None:
        basename, extension = os.path.splitext(os.path.basename(filename))
        title = basename.title().replace('_', ' ')

    # ... and use a url friendly version of that title for the name
    name = slugify(title).replace('-','_')

    # Note that this will replace any existing layer that has the same name
    # with the data that is being passed.
    try:
        layer = Layer.objects.get(name=name)
    except Layer.DoesNotExist, e:
        layer = name

    new_layer = save(layer, filename, theuser, overwrite, keywords=keywords)

    return new_layer


def upload(incoming, user=None, overwrite=True, keywords = []):
    """Upload a directory of spatial data files to GeoNode

       This function also verifies that each layer is in GeoServer.

       Supported extensions are: .shp, .tif, and .zip (of a shapfile).
       It catches GeoNodeExceptions and gives a report per file
       >>> batch_upload('/tmp/mydata')
           [{'file': 'data1.tiff', 'name': 'geonode:data1' },
           {'file': 'data2.shp', 'errors': 'Shapefile requires .prj file'}]
    """
    check_geonode_is_up()

    if os.path.isfile(incoming):
        layer = file_upload(incoming,
                            user=user,
                            overwrite=overwrite,
                            keywords = keywords)
        return [{'file': incoming, 'name': layer.name}]
    elif not os.path.isdir(incoming):
        msg = ('Please pass a filename or a directory name as the "incoming" '
               'parameter, instead of %s: %s' % (incoming, type(incoming)))
        logger.exception(msg)
        raise GeoNodeException(msg)
    else:
        datadir = incoming
        results = []

        for root, dirs, files in os.walk(datadir):
            for short_filename in files:
                basename, extension = os.path.splitext(short_filename)
                filename = os.path.join(root, short_filename)
                if extension in ['.tif', '.shp', '.zip']:
                    try:
                        layer = file_upload(filename,
                                            user=user,
                                            title=basename,
                                            overwrite=overwrite,
                                            keywords=keywords
                                           )

                    except GeoNodeException, e:
                        msg = ('[%s] could not be uploaded. Error was: '
                               '%s' % (filename, str(e)))
                        logger.info(msg)
                        results.append({'file': filename, 'errors': msg})
                    else:
                        results.append({'file': filename, 'name': layer.name})
        return results

def update_metadata(layer_uuid, xml, saved_layer):
    logger.info('>>> Step XML. If an XML metadata document was passed, process it')
    # Step XML.  If an XML metadata document is uploaded,
    # parse the XML metadata and update uuid and URLs as per the content model

    # check if document is XML
    try:
        exml = etree.fromstring(xml)
    except Exception, err:
        raise GeoNodeException('Uploaded XML document is not XML: %s' % str(err))

    # check if document is an accepted XML metadata format
    try:
        tagname = exml.tag.split('}')[1]
    except:
        tagname = exml.tag

    # update relevant XML
    #layer_updated = saved_layer.date.strftime('%Y-%m-%dT%H:%M:%SZ') 
    layer_updated = saved_layer.date.strftime('%Y-%m-%d')

    if tagname != 'MD_Metadata' and settings.CSW['type'] != 'pycsw':
        raise GeoNodeException('Only ISO XML is supported')

    if tagname == 'Record':  # Dublin Core
        dc_ns = '{http://purl.org/dc/elements/1.1/}'
        dct_ns ='{http://purl.org/dc/terms/}'

        children = exml.getchildren()

        # set/update identifier
        xname = exml.find('%sidentifier' % dc_ns)
        if xname is None:  # doesn't exist, insert it
            value = etree.Element('%sidentifier' % dc_ns)
            value.text = layer_uuid
            children.insert(0, value)
        else:  # exists, update it
            xname.text = layer_uuid

        xname = exml.find('%smodified' % dct_ns)
        if xname is None:  # doesn't exist, insert it
            value = etree.Element('%smodified' % dct_ns)
            value.text = layer_updated
            children.insert(3, value)
        else:  # exists, update it
            xname.text = layer_updated

        # set/update URLs
        http_link = etree.Element('%sreferences' % dct_ns, scheme='WWW:LINK-1.0-http--link')
        http_link.text = '%s%s' % (settings.SITEURL, saved_layer.get_absolute_url())
        children.insert(-2, http_link)

        for extension, dformat, protocol, link in saved_layer.download_links():
            http_link = etree.Element('%sreferences' % dct_ns, scheme=protocol)
            http_link.text = link
            children.insert(-2, http_link)

        # set django properties
        csw_exml = CswRecord(exml)
        md_title = csw_exml.title
        md_abstract = csw_exml.abstract

    elif tagname == 'MD_Metadata':
        gmd_ns = 'http://www.isotc211.org/2005/gmd'
        gco_ns = 'http://www.isotc211.org/2005/gco'

        # set/update gmd:fileIdentifier
        xname = exml.find('{%s}fileIdentifier' % gmd_ns)
        if xname is None:  # doesn't exist, insert it
            children = exml.getchildren()
            fileid = etree.Element('{%s}fileIdentifier' % gmd_ns)
            etree.SubElement(fileid, '{%s}CharacterString' % gco_ns).text = layer_uuid
            children.insert(0, fileid)
        else:  # gmd:fileIdentifier exists, check for gco:CharacterString
            value = xname.find('{%s}CharacterString' % gco_ns)
            if value is None:
                etree.SubElement(xname, '{%s}CharacterString' % gco_ns).text = layer_uuid
            else:
                value.text = layer_uuid

        # set/update gmd:dateStamp
        xname = exml.find('{%s}dateStamp' % gmd_ns)
        if xname is None:  # doesn't exist, insert it
            children = exml.getchildren()
            datestamp = etree.Element('{%s}dateStamp' % gmd_ns)
            etree.SubElement(datestamp, '{%s}DateTime' % gco_ns).text = layer_updated
            children.insert(4, datestamp)
        else:  # gmd:dateStamp exists, check for gco:Date or gco:DateTime
            value = xname.find('{%s}Date' % gco_ns)
            value2 = xname.find('{%s}DateTime' % gco_ns)

            if value is None and value2 is not None:  # set gco:DateTime
                value2.text = layer_updated
            elif value is not None and value2 is None:  # set gco:Date
                value.text = saved_layer.date.strftime('%Y-%m-%d')
            elif value is None and value2 is None:
                etree.SubElement(xname, '{%s}DateTime' % gco_ns).text = layer_updated

        # set/update URLs
        children = exml.getchildren()
        distinfo = etree.Element('{%s}distributionInfo' % gmd_ns)
        distinfo2 = etree.SubElement(distinfo, '{%s}MD_Distribution' % gmd_ns)
        transopts = etree.SubElement(distinfo2, '{%s}transferOptions' % gmd_ns)
        transopts2 = etree.SubElement(transopts, '{%s}MD_DigitalTransferOptions' % gmd_ns)

        online = etree.SubElement(transopts2, '{%s}onLine' % gmd_ns)
        online2 = etree.SubElement(online, '{%s}CI_OnlineResource' % gmd_ns)
        linkage = etree.SubElement(online2, '{%s}linkage' % gmd_ns)
        etree.SubElement(linkage, '{%s}URL' % gmd_ns).text = '%s%s' % (settings.SITEURL, saved_layer.get_absolute_url())
        protocol = etree.SubElement(online2, '{%s}protocol' % gmd_ns)
        etree.SubElement(protocol, '{%s}CharacterString' % gco_ns).text = 'WWW:LINK-1.0-http--link'

        for extension, dformat, dprotocol, link in saved_layer.download_links():
            online = etree.SubElement(transopts2, '{%s}onLine' % gmd_ns)
            online2 = etree.SubElement(online, '{%s}CI_OnlineResource' % gmd_ns)
            linkage = etree.SubElement(online2, '{%s}linkage' % gmd_ns)

            etree.SubElement(linkage, '{%s}URL' % gmd_ns).text = link or ''
            protocol = etree.SubElement(online2, '{%s}protocol' % gmd_ns)
            etree.SubElement(protocol, '{%s}CharacterString' % gco_ns).text = dprotocol
            lname = etree.SubElement(online2, '{%s}name' % gmd_ns)
            etree.SubElement(lname, '{%s}CharacterString' % gco_ns).text = extension or ''
            ldesc = etree.SubElement(online2, '{%s}description' % gmd_ns)
            etree.SubElement(ldesc, '{%s}CharacterString' % gco_ns).text = dformat or ''

        children.insert(-2, distinfo)

        iso_exml = MD_Metadata(exml)
        md_title = iso_exml.identification.title
        md_abstract = iso_exml.identification.abstract

    elif tagname == 'metadata':  # FGDC
        # set/update identifier
        xname = exml.find('idinfo/datasetid')
        if xname is None:  # doesn't exist, insert it
            children = exml.find('idinfo').getchildren()
            value = etree.Element('datasetid')
            value.text = layer_uuid
            children.insert(0, value)
        else:  # exists, update it
            xname.text = layer_uuid

        xname = exml.find('metainfo/metd')
        if xname is None:  # doesn't exist, insert it
            value = etree.Element('metd')
            value.text = layer_updated
            exml.find('metainfo').append(value)
        else:  # exists, update it
            xname.text = layer_updated

        # set/update URLs
        citeinfo = exml.find('idinfo/citation/citeinfo')
        http_link = etree.Element('onlink')
        http_link.attrib['type'] = 'WWW:LINK-1.0-http--link'
        http_link.text = '%s%s' % (settings.SITEURL, saved_layer.get_absolute_url())
        citeinfo.append(http_link)

        for extension, dformat, protocol, link in saved_layer.download_links():
            http_link = etree.Element('onlink')
            http_link.attrib['type'] = protocol
            http_link.text = link
            citeinfo.append(http_link)

        fgdc_exml = Metadata(exml)
        from owslib.fgdc import Metadata as FGDC_Metadata


        with open('/tmp/fff.txt','w') as ff:
            ff.write(str(etree.tostring(exml)))

        fgdc_exml = FGDC_Metadata(exml)
        md_title = fgdc_exml.idinfo.citation.citeinfo['title'] 
        md_abstract = fgdc_exml.idinfo.descript.abstract

    else:
        raise GeoNodeException('Unsupported metadata format')

    return [etree.tostring(exml), md_title, md_abstract]

def set_metadata(xml, saved_layer):
    """Parse and set metadata to GeoNode Django ORM"""

    # check if document is XML
    try:
        exml = etree.fromstring(xml)
    except Exception, err:
        raise GeoNodeException('Uploaded XML document is not XML: %s' % str(err))

    # check if document is an accepted XML metadata format
    try:
        tagname = exml.tag.split('}')[1]
    except:
        tagname = exml.tag

    vals = {}
    vals['csw_mdsource'] = 'local'

    if tagname == 'MD_Metadata':
        md = MD_Metadata(exml)

        vals['csw_typename'] =  'gmd:MD_Metadata'
        vals['csw_schema'] = 'http://www.isotc211.org/2005/gmd'
        vals['language'] = md.language
        vals['spatial_representation_type'] = md.hierarchy
        vals['date'] = sniff_date(md.datestamp.strip())

        if hasattr(md, 'identification'):
            vals['temporal_extent_start'] = md.identification.temporalextent_start
            vals['temporal_extent_end'] = md.identification.temporalextent_end

            if len(md.identification.topiccategory) > 0:
                vals['topic_category'] = md.identification.topiccategory[0]

            if (hasattr(md.identification, 'keywords') and
            len(md.identification.keywords) > 0):
                if None not in md.identification.keywords[0]['keywords']:
                    vals['keywords'] = ','.join(md.identification.keywords[0]['keywords'])

            if hasattr(md.identification, 'creator'):
                vals['creator'] = md.identification.creator
            if hasattr(md.identification, 'publisher'):
                vals['publisher'] = md.identification.publisher

            if len(md.identification.securityconstraints) > 0:
                vals['constraints_use'] = md.identification.securityconstraints[0]
            if len(md.identification.accessconstraints) > 0:
                vals['constraints_access'] = md.identification.accessconstraints[0]
            if len(md.identification.otherconstraints) > 0:
                vals['constraints_other'] = md.identification.otherconstraints[0]

            vals['purpose'] = md.identification.purpose

        if hasattr(md.identification, 'dataquality'):     
            vals['data_quality_statement'] = md.dataquality.lineage

    elif tagname == 'metadata':  # FGDC
        md = Metadata(exml)

        vals['csw_typename'] = 'fgdc:metadata'
        vals['csw_schema'] = 'http://www.opengis.net/cat/csw/csdgm'
        vals['spatial_representation_type'] = md.idinfo.citation.citeinfo['geoform']

        if hasattr(md.idinfo, 'keywords'):
            if md.idinfo.keywords.theme:
                vals['keywords'] = ','.join(md.idinfo.keywords.theme[0]['themekey'])

        if hasattr(md.idinfo.timeperd, 'timeinfo'):
            if hasattr(md.idinfo.timeperd.timeinfo, 'rngdates'):
                vals['temporal_extent_start'] = sniff_date(md.idinfo.timeperd.timeinfo.rngdates.begdate.strip())
                vals['temporal_extent_end'] = sniff_date(md.idinfo.timeperd.timeinfo.rngdates.enddate.strip())

        if hasattr(md.idinfo, 'origin'):
            vals['creator'] = md.idinfo.origin
            vals['publisher'] = md.idinfo.origin

        vals['constraints_access'] = md.idinfo.accconst
        vals['constraints_other'] = md.idinfo.useconst
        #vals['date'] = sniff_date(md.metainfo.metd.strip())
        vals['date'] = '2012-01-23'

    elif tagname == 'Record':  # Dublin Core
        md = CswRecord(exml)

	vals['csw_typename'] = 'csw:Record'
	vals['csw_schema'] = 'http://www.opengis.net/cat/csw/2.0.2'
	vals['language'] = md.language
	vals['spatial_representation_type'] = md.type
	vals['keywords'] = ','.join(md.subjects)
	vals['temporal_extent_start'] = md.temporal
	vals['temporal_extent_end'] = md.temporal
	vals['creator'] = md.creator
	vals['publisher'] = md.publisher
	vals['constraints_access'] = md.accessrights
	vals['constraints_other'] = md.license
	vals['date'] = sniff_date(md.modified.strip())

    else:
        raise RuntimeError('Unsupported metadata format')

    return vals

def _create_db_featurestore(name, data, overwrite = False, charset = None):
    """Create a database store then use it to import a shapefile.

    If the import into the database fails then delete the store
    (and delete the PostGIS table for it).
    """
    cat = Layer.objects.gs_catalog
    try:
        ds = cat.get_store(settings.DB_DATASTORE_NAME)
    except FailedRequestError, e:
        ds = cat.create_datastore(settings.DB_DATASTORE_NAME)
        ds.connection_parameters.update(
            host=settings.DB_DATASTORE_HOST,
            port=settings.DB_DATASTORE_PORT,
            database=settings.DB_DATASTORE_DATABASE,
            user=settings.DB_DATASTORE_USER,
            passwd=settings.DB_DATASTORE_PASSWORD,
            dbtype=settings.DB_DATASTORE_TYPE)
        cat.save(ds)
        ds = cat.get_store(settings.DB_DATASTORE_NAME)

    try:
        cat.add_data_to_store(ds, name, data, overwrite, charset)
        return ds, cat.get_resource(name, store=ds)
    except:
        delete_from_postgis(name)
        raise

def forward_mercator(lonlat):
    """
        Given geographic coordinates, return a x,y tuple in spherical mercator.
        
        If the lat value is out of range, -inf will be returned as the y value
    """
    x = lonlat[0] * 20037508.34 / 180
    n = math.tan((90 + lonlat[1]) * math.pi / 360)
    if n <= 0:
        y = float("-inf")
    else:
        y = math.log(n) / math.pi * 20037508.34
    return (x, y)

def inverse_mercator(xy):
    """
        Given coordinates in spherical mercator, return a lon,lat tuple.
    """
    lon = (xy[0] / 20037508.34) * 180
    lat = (xy[1] / 20037508.34) * 180
    lat = 180/math.pi * (2 * math.atan(math.exp(lat * math.pi / 180)) - math.pi / 2)
    return (lon, lat)

def sniff_date(datestr):
    """Attempt to parse date into datetime.datetime object"""

    fmt_str = None

    if len(datestr) == 8:  # '20001122'
        datestr = '%s-%s-%s' % (datestr[:4], datestr[4:6], datestr[-2:])
        fmt_str = '%Y-%m-%d'
    elif len(datestr) == 10: # '2000-11-22'
        fmt_str = '%Y-%m-%d'
    elif datestr.find('Z') != -1: # '2000-11-22T11:11:11Z'
        fmt_str = '%Y-%m-%dT%H:%M:%SZ'
    elif datestr.find('T') != -1: # '2000-11-22T'
        fmt_str = '%Y-%m-%dT'
    else:
        return None

    return datetime.datetime.strptime(datestr, fmt_str)
