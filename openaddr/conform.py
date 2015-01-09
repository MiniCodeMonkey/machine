import os
import errno
import tempfile
import unicodecsv
import json
import copy

import logging
_L = logging.getLogger(__name__)

from zipfile import ZipFile
from argparse import ArgumentParser

from .sample import sample_geojson
from .expand import expand_street_name

from osgeo import ogr, osr
ogr.UseExceptions()

geometry_types = {
    ogr.wkbPoint: 'Point',
    ogr.wkbPoint25D: 'Point 2.5D',
    ogr.wkbLineString: 'LineString',
    ogr.wkbLineString25D: 'LineString 2.5D',
    ogr.wkbLinearRing: 'LinearRing',
    ogr.wkbPolygon: 'Polygon',
    ogr.wkbPolygon25D: 'Polygon 2.5D',
    ogr.wkbMultiPoint: 'MultiPoint',
    ogr.wkbMultiPoint25D: 'MultiPoint 2.5D',
    ogr.wkbMultiLineString: 'MultiLineString',
    ogr.wkbMultiLineString25D: 'MultiLineString 2.5D',
    ogr.wkbMultiPolygon: 'MultiPolygon',
    ogr.wkbMultiPolygon25D: 'MultiPolygon 2.5D',
    ogr.wkbGeometryCollection: 'GeometryCollection',
    ogr.wkbGeometryCollection25D: 'GeometryCollection 2.5D',
    ogr.wkbUnknown: 'Unknown'
    }

def mkdirsp(path):
    try:
        os.makedirs(path)
    except OSError as exc:
        if exc.errno == errno.EEXIST and os.path.isdir(path):
            pass
        else:
            raise


class ConformResult:
    processed = None
    sample = None
    geometry_type = None
    path = None
    elapsed = None
    
    # needed by openaddr.process.write_state(), for now.
    output = ''

    def __init__(self, processed, sample, geometry_type, path, elapsed):
        self.processed = processed
        self.sample = sample
        self.geometry_type = geometry_type
        self.path = path
        self.elapsed = elapsed

    @staticmethod
    def empty():
        return ConformResult(None, None, None, None, None)

    def todict(self):
        return dict(processed=self.processed, sample=self.sample)


class DecompressionError(Exception):
    pass


class DecompressionTask(object):
    @classmethod
    def from_type_string(clz, type_string):
        if type_string == None:
            return NoopDecompressTask()
        elif type_string.lower() == 'zip':
            return ZipDecompressTask()
        else:
            raise KeyError("I don't know how to decompress for type {}".format(type_string))

    def decompress(self, source_paths):
        raise NotImplementedError()


class NoopDecompressTask(DecompressionTask):
    def decompress(self, source_paths, workdir):
        return source_paths


class ZipDecompressTask(DecompressionTask):
    def decompress(self, source_paths, workdir):
        output_files = []
        expand_path = os.path.join(workdir, 'unzipped')
        mkdirsp(expand_path)

        for source_path in source_paths:
            with ZipFile(source_path, 'r') as z:
                for name in z.namelist():
                    expanded_file_path = z.extract(name, expand_path)
                    _L.debug("Expanded file %s", expanded_file_path)
                    output_files.append(expanded_file_path)
        return output_files

class ExcerptDataTask(object):
    ''' Task for sampling three rows of data from datasource.
    '''
    known_types = ('.shp', '.json', '.csv', '.kml')

    def excerpt(self, source_paths, workdir):
        '''
        
            Tested version from openaddr.excerpt() on master branch:

            if ext == '.zip':
                _L.debug('Downloading all of {cache}'.format(**extras))

                with open(cachefile, 'w') as file:
                    for chunk in got.iter_content(1024**2):
                        file.write(chunk)
    
                zf = ZipFile(cachefile, 'r')
        
                for name in zf.namelist():
                    _, ext = splitext(name)
            
                    if ext in ('.shp', '.shx', '.dbf'):
                        with open(join(workdir, 'cache'+ext), 'w') as file:
                            file.write(zf.read(name))
        
                if exists(join(workdir, 'cache.shp')):
                    ds = ogr.Open(join(workdir, 'cache.shp'))
                else:
                    ds = None
    
            elif ext == '.json':
                _L.debug('Downloading part of {cache}'.format(**extras))

                scheme, host, path, query, _, _ = urlparse(got.url)
        
                if scheme in ('http', 'https'):
                    conn = HTTPConnection(host, 80)
                    conn.request('GET', path + ('?' if query else '') + query)
                    resp = conn.getresponse()
                elif scheme == 'file':
                    with open(path) as rawfile:
                        resp = StringIO(rawfile.read(1024*1024))
                else:
                    raise RuntimeError('Unsure what to do with {}'.format(got.url))
        
                with open(cachefile, 'w') as file:
                    file.write(sample_geojson(resp, 10))
    
                ds = ogr.Open(cachefile)
    
            else:
                ds = None
        '''
        known_paths = [source_path for source_path in source_paths
                       if os.path.splitext(source_path)[1] in self.known_types]
        
        if not known_paths:
            # we know nothing.
            return None
        
        data_path = known_paths[0]

        # Sample a few GeoJSON features to save on memory for large datasets.
        if os.path.splitext(data_path)[1] == '.json':
            with open(data_path, 'r') as complete_layer:
                temp_dir = os.path.dirname(data_path)
                _, temp_path = tempfile.mkstemp(dir=temp_dir, suffix='.json')

                with open(temp_path, 'w') as temp_file:
                    temp_file.write(sample_geojson(complete_layer, 10))
                    data_path = temp_path
        
        datasource = ogr.Open(data_path, 0)
        layer = datasource.GetLayer()

        layer_defn = layer.GetLayerDefn()
        fieldnames = [layer_defn.GetFieldDefn(i).GetName()
                      for i in range(layer_defn.GetFieldCount())]

        data_sample = [fieldnames]
        
        for feature in layer:
            data_sample.append([feature.GetField(i) for i
                                in range(layer_defn.GetFieldCount())])

            if len(data_sample) == 6:
                break
        
        geometry_type = geometry_types.get(layer_defn.GetGeomType(), None)

        return data_sample, geometry_type

def find_source_path(source_definition, source_paths):
    "Figure out which of the possible paths is the actual source"
    conform = source_definition["conform"]
    if conform["type"] in ("shapefile", "shapefile-polygon"):
        # Shapefiles are named *.shp
        candidates = []
        for fn in source_paths:
            basename, ext = os.path.splitext(fn)
            if ext.lower() == ".shp":
                candidates.append(fn)
        if len(candidates) == 0:
            _L.warning("No shapefiles found in %s", source_paths)
            return None
        elif len(candidates) == 1:
            _L.debug("Selected %s for source", candidates[0])
            return candidates[0]
        else:
            # Multiple candidates; look for the one named by the file attribute
            if not conform.has_key("file"):
                _L.warning("Multiple shapefiles found, but source has no file attribute.")
                return None
            source_file_name = conform["file"]
            for c in candidates:
                if source_file_name == os.path.basename(c):
                    return c
            _L.warning("Source names file %s but could not find it", source_file_name)
            return None
    elif conform["type"] == "geojson":
        candidates = []
        for fn in source_paths:
            basename, ext = os.path.splitext(fn)
            if ext.lower() == ".json":
                candidates.append(fn)
        if len(candidates) == 0:
            _L.warning("No JSON found in %s", source_paths)
            return None
        elif len(candidates) == 1:
            _L.debug("Selected %s for source", candidates[0])
            return candidates[0]
        else:
            _L.warning("Found more than one JSON file in source, can't pick one")
            # geojson spec currently doesn't include a file attribute. Maybe it should?
            return None
    elif conform["type"] == "csv":
        # We don't expect to be handed a list of files
        return source_paths[0]
    else:
        _L.warning("Unknown source type %s", conform["type"])
        return None

class ConvertToCsvTask(object):
    known_types = ('.shp', '.json', '.csv', '.kml')

    def convert(self, source_definition, source_paths, workdir):
        "Convert a list of source_paths and write results in workdir"
        _L.debug("Converting to %s", workdir)

        # Create a subdirectory "converted" to hold results
        output_file = None
        convert_path = os.path.join(workdir, 'converted')
        mkdirsp(convert_path)

        # Find the source and convert it
        source_path = find_source_path(source_definition, source_paths)
        if source_path is not None:
            basename, ext = os.path.splitext(os.path.basename(source_path))
            dest_path = os.path.join(convert_path, basename + ".csv")
            rc = conform_cli(source_definition, source_path, dest_path)
            if rc == 0:
                # Success! Return the path of the output CSV
                return dest_path

        # Conversion must have failed
        return None

def ogr_source_to_csv(source_path, dest_path):
    "Convert a single shapefile or GeoJSON in source_path and put it in dest_path"
    in_datasource = ogr.Open(source_path, 0)
    in_layer = in_datasource.GetLayer()
    inSpatialRef = in_layer.GetSpatialRef()

    _L.info("Converting a layer to CSV: %s", in_layer.GetName())

    in_layer_defn = in_layer.GetLayerDefn()
    out_fieldnames = []
    for i in range(0, in_layer_defn.GetFieldCount()):
        field_defn = in_layer_defn.GetFieldDefn(i)
        out_fieldnames.append(field_defn.GetName())
    out_fieldnames.append('X')
    out_fieldnames.append('Y')

    outSpatialRef = osr.SpatialReference()
    outSpatialRef.ImportFromEPSG(4326)
    coordTransform = osr.CoordinateTransformation(inSpatialRef, outSpatialRef)

    with open(dest_path, 'wb') as f:
        writer = unicodecsv.DictWriter(f, fieldnames=out_fieldnames, encoding='utf-8')
        writer.writeheader()

        in_feature = in_layer.GetNextFeature()
        while in_feature:
            row = dict()

            for i in range(0, in_layer_defn.GetFieldCount()):
                field_defn = in_layer_defn.GetFieldDefn(i)
                row[field_defn.GetNameRef()] = in_feature.GetField(i)
            geom = in_feature.GetGeometryRef()
            geom.Transform(coordTransform)
            # Calculate the centroid of the geometry and write it as X and Y columns
            centroid = geom.Centroid()
            row['X'] = centroid.GetX()
            row['Y'] = centroid.GetY()

            writer.writerow(row)

            in_feature.Destroy()
            in_feature = in_layer.GetNextFeature()

    in_datasource.Destroy()

def csv_source_to_csv(source_definition, source_path, dest_path):
    "Convert a source CSV file to an intermediate form, coerced to UTF-8 and EPSG:4326"
    _L.info("Converting source CSV %s", source_path)

    # TODO: extra features of CSV sources.
    for unimplemented in ("encoding", "headers", "skiplines"):
        assert not source_definition["conform"].has_key(unimplemented)

    delim = source_definition["conform"].get("csvsplit", ",")
    # Python2 unicodecsv requires this be a string, not unicode.
    delim = delim.encode('ascii')

    # Extract the source CSV, applying conversions to deal with oddball CSV formats
    # Also convert encoding to utf-8 and reproject to EPSG:4326 in X and Y columns
    with open(source_path, 'rb') as source_fp:
        reader = unicodecsv.DictReader(source_fp, encoding='utf-8', delimiter = delim)

        # Construct headers for the extracted CSV file
        old_latlon = (source_definition["conform"]["lat"], source_definition["conform"]["lon"])
        out_fieldnames = [fn for fn in reader.fieldnames if fn not in old_latlon]
        out_fieldnames.append("X")
        out_fieldnames.append("Y")

        # Write the extracted CSV file
        with open(dest_path, 'wb') as dest_fp:
            writer = unicodecsv.DictWriter(dest_fp, out_fieldnames)
            writer.writeheader()
            # For every row in the source CSV
            for source_row in reader:
                out_row = row_extract_and_reproject(source_definition, source_row)
                writer.writerow(out_row)

def row_extract_and_reproject(source_definition, source_row):
    """Find lat/lon in source CSV data and store it in ESPG:4326 in X/Y in the row"""
    lat_name = source_definition["conform"]["lat"]
    lon_name = source_definition["conform"]["lon"]
    out_row = copy.deepcopy(source_row)
    out_row["X"] = source_row[lon_name]
    del out_row[lon_name]
    out_row["Y"] = source_row[lat_name]
    del out_row[lat_name]
    return out_row

### Row-level conform code. Inputs and outputs are individual rows in a CSV file.
### The input row may or may not be modified in place. The output row is always returned.

def row_transform_and_convert(sd, row):
    "Apply the full conform transform and extract operations to a row"

    # Some conform specs have fields named with a case different from the source
    row = row_smash_case(sd, row)

    c = sd["conform"]
    if c.has_key("merge"):
        row = row_merge_street(sd, row)
    if c.has_key("advanced_merge"):
        row = row_advanced_merge(sd, row)
    if c.has_key("split"):
        row = row_split_address(sd, row)
    row = row_convert_to_out(sd, row)
    row = row_canonicalize_street_and_number(sd, row)
    return row

def conform_smash_case(source_definition):
    "Convert all named fields in source_definition object to lowercase. Returns new object."
    new_sd = copy.deepcopy(source_definition)
    conform = new_sd["conform"]
    for k in ("split", "lat", "lon", "street", "number"):
        if conform.has_key(k):
            conform[k] = conform[k].lower()
    if conform.has_key("merge"):
        conform["merge"] = [s.lower() for s in conform["merge"]]
    return new_sd

def row_smash_case(sd, row):
    "Convert all field names to lowercase. Slow, but necessary for imprecise conform specs."
    row = { k.lower(): v for (k, v) in row.items() }
    return row

def row_merge_street(sd, row):
    "Merge multiple columns like 'Maple','St' to 'Maple St'"
    merge_data = [row[field] for field in sd["conform"]["merge"]]
    row['auto_street'] = ' '.join(merge_data)
    return row

def row_advanced_merge(sd, row):
    assert False

def row_split_address(sd, row):
    "Split addresses like '123 Maple St' into '123' and 'Maple St'"
    cols = row[sd["conform"]["split"]].split(' ', 1)  # maxsplit
    row['auto_number'] = cols[0]
    row['auto_street'] = cols[1] if len(cols) > 1 else ''
    return row

def row_canonicalize_street_and_number(sd, row):
    "Expand abbreviations and otherwise canonicalize street name and number"
    row["NUMBER"] = row["NUMBER"].strip()
    row["STREET"] = expand_street_name(row["STREET"])
    return row

def row_convert_to_out(sd, row):
    "Convert a row from the source schema to OpenAddresses output schema"
    # note: sd["conform"]["lat"] and lon were already applied in the extraction from source
    return {
        "LON": row.get("x", None),
        "LAT": row.get("y", None),
        "NUMBER": row.get(sd["conform"]["number"], None),
        "STREET": row.get(sd["conform"]["street"], None)
    }

### File-level conform code. Inputs and outputs are filenames.

def extract_to_source_csv(source_definition, source_path, extract_path):
    """Extract arbitrary downloaded sources to an extracted CSV in the source schema.
    source_definition: description of the source, containing the conform object
    extract_path: file to write the extracted CSV file

    The extracted file will be in UTF-8 and will have X and Y columns corresponding
    to longitude and latitude in EPSG:4326.
    """
    # TODO: handle non-SHP sources
    if source_definition["conform"]["type"] in ("shapefile", "shapefile-polygon", "geojson"):
        ogr_source_to_csv(source_path, extract_path)
    elif source_definition["conform"]["type"] == "csv":
        csv_source_to_csv(source_definition, source_path, extract_path)
    else:
        raise Exception("Unsupported source type %s" % source_definition["conform"]["type"])

# The canonical output schema for conform
_openaddr_csv_schema = ["LON", "LAT", "NUMBER", "STREET"]

def transform_to_out_csv(source_definition, extract_path, dest_path):
    """Transform an extracted source CSV to the OpenAddresses output CSV by applying conform rules
    source_definition: description of the source, containing the conform object
    extract_path: extracted CSV file to process
    dest_path: path for output file in OpenAddress CSV
    """

    # Convert all field names in the conform spec to lower case
    source_definition = conform_smash_case(source_definition)

    # Read through the extract CSV
    with open(extract_path, 'rb') as extract_fp:
        reader = unicodecsv.DictReader(extract_fp, encoding='utf-8')
        # Write to the destination CSV
        with open(dest_path, 'wb') as dest_fp:
            writer = unicodecsv.DictWriter(dest_fp, _openaddr_csv_schema)
            writer.writeheader()
            # For every row in the extract
            for extract_row in reader:
                out_row = row_transform_and_convert(source_definition, extract_row)
                writer.writerow(out_row)

def conform_cli(source_definition, source_path, dest_path):
    "Command line entry point for conforming a downloaded source to an output CSV."
    # TODO: this tool only works if the source creates a single output

    if not source_definition.has_key("conform"):
        return 1
    if not source_definition["conform"].get("type", None) in ["shapefile", "shapefile-polygon", "geojson", "csv"]:
        _L.warn("Skipping file with unknown conform: %s", source_path)
        return 1

    # Create a temporary filename for the intermediate extracted source CSV
    fd, extract_path = tempfile.mkstemp(prefix='openaddr-extracted-', suffix='.csv')
    os.close(fd)
    _L.debug('extract temp file %s', extract_path)

    try:
        extract_to_source_csv(source_definition, source_path, extract_path)
        transform_to_out_csv(source_definition, extract_path, dest_path)
    finally:
        os.remove(extract_path)

    return 0

def main():
    "Main entry point for openaddr-pyconform command line tool. (See setup.py)"

    parser = ArgumentParser(description='Conform a downloaded source file.')
    parser.add_argument('source_json', help='Required source JSON file name.')
    parser.add_argument('source_path', help='Required pathname to the actual source data file')
    parser.add_argument('dest_path', help='Required pathname, output file written here.')
    parser.add_argument('-l', '--logfile', help='Optional log file name.')
    parser.add_argument('-v', '--verbose', help='Turn on verbose logging', action="store_true")
    args = parser.parse_args()

    from .jobs import setup_logger
    setup_logger(logfile = args.logfile, log_level = logging.DEBUG if args.verbose else logging.WARNING)

    source_definition = json.load(file(args.source_json))
    rc = conform_cli(source_definition, args.source_path, args.dest_path)
    return rc

if __name__ == '__main__':
    exit(main())


# Test suite. This code could be in a separate file

import unittest, tempfile, shutil

class TestConformTransforms (unittest.TestCase):
    "Test low level data transform functions"

    def test_row_smash_case(self):
        r = row_smash_case(None, {"UPPER": "foo", "lower": "bar", "miXeD": "mixed"})
        self.assertEqual({"upper": "foo", "lower": "bar", "mixed": "mixed"}, r)

    def test_conform_smash_case(self):
        d = { "conform": { "street": "MiXeD", "number": "U", "split": "U", "merge": [ "U", "l", "MiXeD" ], "lat": "Y", "lon": "x" } }
        r = conform_smash_case(d)
        self.assertEqual({ "conform": { "street": "mixed", "number": "u", "split": "u", "merge": [ "u", "l", "mixed" ], "lat": "y", "lon": "x" } }, r)

    def test_row_convert_to_out(self):
        d = { "conform": { "street": "s", "number": "n", "lon": "x", "lat": "y" } }
        r = row_convert_to_out(d, {"s": "MAPLE LN", "n": "123", "x": "-119.2", "y": "39.3"})
        self.assertEqual({"LON": "-119.2", "LAT": "39.3", "STREET": "MAPLE LN", "NUMBER": "123"}, r)

    def test_row_merge_street(self):
        d = { "conform": { "merge": [ "n", "t" ] } }
        r = row_merge_street(d, {"n": "MAPLE", "t": "ST", "x": "foo"})
        self.assertEqual({"auto_street": "MAPLE ST", "x": "foo", "t": "ST", "n": "MAPLE"}, r)

    def test_split_address(self):
        d = { "conform": { "split": "ADDRESS" } }
        r = row_split_address(d, { "ADDRESS": "123 MAPLE ST" })
        self.assertEqual({"ADDRESS": "123 MAPLE ST", "auto_street": "MAPLE ST", "auto_number": "123"}, r)
        r = row_split_address(d, { "ADDRESS": "265" })
        self.assertEqual(r["auto_number"], "265")
        self.assertEqual(r["auto_street"], "")
        r = row_split_address(d, { "ADDRESS": "" })
        self.assertEqual(r["auto_number"], "")
        self.assertEqual(r["auto_street"], "")

    def test_transform_and_convert(self):
        d = { "conform": { "street": "auto_street", "number": "n", "merge": ["s1", "s2"], "lon": "y", "lat": "x" } }
        r = row_transform_and_convert(d, { "n": "123", "s1": "MAPLE", "s2": "ST", "X": "-119.2", "Y": "39.3" })
        self.assertEqual({"STREET": "Maple Street", "NUMBER": "123", "LON": "-119.2", "LAT": "39.3"}, r)

        d = { "conform": { "street": "auto_street", "number": "auto_number", "split": "s", "lon": "y", "lat": "x" } }
        r = row_transform_and_convert(d, { "s": "123 MAPLE ST", "X": "-119.2", "Y": "39.3" })
        self.assertEqual({"STREET": "Maple Street", "NUMBER": "123", "LON": "-119.2", "LAT": "39.3"}, r)

    def test_row_canonicalize_street_and_number(self):
        r = row_canonicalize_street_and_number({}, {"NUMBER": "324 ", "STREET": " OAK DR."})
        self.assertEqual("324", r["NUMBER"])
        self.assertEqual("Oak Drive", r["STREET"])

    def test_row_extract_and_reproject(self):
        d = { "conform" : { "lon": "longitude", "lat": "latitude" } }
        r = row_extract_and_reproject(d, {"longitude": "-122.3", "latitude": "39.1"})
        self.assertEqual({"Y": "39.1", "X": "-122.3"}, r)


class TestConformCli (unittest.TestCase):
    "Test the command line interface creates valid output files from test input"
    def setUp(self):
        self.testdir = tempfile.mkdtemp(prefix='openaddr-testPyConformCli-')
        self.conforms_dir = os.path.join(os.path.dirname(__file__), '..', 'tests', 'conforms')

    def tearDown(self):
        shutil.rmtree(self.testdir)

    def _run_conform_on_source(self, source_name, ext):
        "Helper method to run a conform on the named source. Assumes naming convention."
        source_definition = json.load(file(os.path.join(self.conforms_dir, "%s.json" % source_name)))
        source_path = os.path.join(self.conforms_dir, "%s.%s" % (source_name, ext))
        dest_path = os.path.join(self.testdir, '%s-conformed.csv' % source_name)

        rc = conform_cli(source_definition, source_path, dest_path)
        return rc, dest_path

    def test_unknown_conform(self):
        # Test that the conform tool does something reasonable with unknown conform sources
        self.assertEqual(1, conform_cli({}, 'test', ''))
        self.assertEqual(1, conform_cli({'conform': {}}, 'test', ''))
        self.assertEqual(1, conform_cli({'conform': {'type': 'broken'}}, 'test', ''))

    def test_lake_man(self):
        rc, dest_path = self._run_conform_on_source('lake-man', 'shp')
        self.assertEqual(0, rc)

        with open(dest_path) as fp:
            reader = unicodecsv.DictReader(fp)
            self.assertEqual(['LON', 'LAT', 'NUMBER', 'STREET'], reader.fieldnames)

            rows = list(reader)

            self.assertAlmostEqual(float(rows[0]['LAT']), 37.802612637607439)
            self.assertAlmostEqual(float(rows[0]['LON']), -122.259249687194824)

            self.assertEqual(rows[0]['NUMBER'], '5115')
            self.assertEqual(rows[0]['STREET'], 'Fruited Plains Lane')
            self.assertEqual(rows[1]['NUMBER'], '5121')
            self.assertEqual(rows[1]['STREET'], 'Fruited Plains Lane')
            self.assertEqual(rows[2]['NUMBER'], '5133')
            self.assertEqual(rows[2]['STREET'], 'Fruited Plains Lane')
            self.assertEqual(rows[3]['NUMBER'], '5126')
            self.assertEqual(rows[3]['STREET'], 'Fruited Plains Lane')
            self.assertEqual(rows[4]['NUMBER'], '5120')
            self.assertEqual(rows[4]['STREET'], 'Fruited Plains Lane')
            self.assertEqual(rows[5]['NUMBER'], '5115')
            self.assertEqual(rows[5]['STREET'], 'Old Mill Road')

    def test_lake_man_split(self):
        rc, dest_path = self._run_conform_on_source('lake-man-split', 'shp')
        self.assertEqual(0, rc)
        
        with open(dest_path) as fp:
            rows = list(unicodecsv.DictReader(fp))
            self.assertEqual(rows[0]['NUMBER'], '915')
            self.assertEqual(rows[0]['STREET'], 'Edward Avenue')
            self.assertEqual(rows[1]['NUMBER'], '3273')
            self.assertEqual(rows[1]['STREET'], 'Peter Street')
            self.assertEqual(rows[2]['NUMBER'], '976')
            self.assertEqual(rows[2]['STREET'], 'Ford Boulevard')
            self.assertEqual(rows[3]['NUMBER'], '7055')
            self.assertEqual(rows[3]['STREET'], 'Saint Rose Avenue')
            self.assertEqual(rows[4]['NUMBER'], '534')
            self.assertEqual(rows[4]['STREET'], 'Wallace Avenue')
            self.assertEqual(rows[5]['NUMBER'], '531')
            self.assertEqual(rows[5]['STREET'], 'Scofield Avenue')

    def test_lake_man_merge_postcode(self):
        rc, dest_path = self._run_conform_on_source('lake-man-merge-postcode', 'shp')
        self.assertEqual(0, rc)
        
        with open(dest_path) as fp:
            rows = list(unicodecsv.DictReader(fp))
            self.assertEqual(rows[0]['NUMBER'], '35845')
            self.assertEqual(rows[0]['STREET'], 'Eklutna Lake Road')
            self.assertEqual(rows[1]['NUMBER'], '35850')
            self.assertEqual(rows[1]['STREET'], 'Eklutna Lake Road')
            self.assertEqual(rows[2]['NUMBER'], '35900')
            self.assertEqual(rows[2]['STREET'], 'Eklutna Lake Road')
            self.assertEqual(rows[3]['NUMBER'], '35870')
            self.assertEqual(rows[3]['STREET'], 'Eklutna Lake Road')
            self.assertEqual(rows[4]['NUMBER'], '32551')
            self.assertEqual(rows[4]['STREET'], 'Eklutna Lake Road')
            self.assertEqual(rows[5]['NUMBER'], '31401')
            self.assertEqual(rows[5]['STREET'], 'Eklutna Lake Road')
    
    def test_lake_man_merge_postcode2(self):
        rc, dest_path = self._run_conform_on_source('lake-man-merge-postcode2', 'shp')
        self.assertEqual(0, rc)
        
        with open(dest_path) as fp:
            rows = list(unicodecsv.DictReader(fp))
            self.assertEqual(rows[0]['NUMBER'], '85')
            self.assertEqual(rows[0]['STREET'], 'Maitland Drive')
            self.assertEqual(rows[1]['NUMBER'], '81')
            self.assertEqual(rows[1]['STREET'], 'Maitland Drive')
            self.assertEqual(rows[2]['NUMBER'], '92')
            self.assertEqual(rows[2]['STREET'], 'Maitland Drive')
            self.assertEqual(rows[3]['NUMBER'], '92')
            self.assertEqual(rows[3]['STREET'], 'Maitland Drive')
            self.assertEqual(rows[4]['NUMBER'], '92')
            self.assertEqual(rows[4]['STREET'], 'Maitland Drive')
            self.assertEqual(rows[5]['NUMBER'], '92')
            self.assertEqual(rows[5]['STREET'], 'Maitland Drive')

    def test_lake_man_shp_utf8(self):
        rc, dest_path = self._run_conform_on_source('lake-man-utf8', 'shp')
        self.assertEqual(0, rc)
        with open(dest_path) as fp:
            rows = list(unicodecsv.DictReader(fp, encoding='utf-8'))
            self.assertEqual(rows[0]['STREET'], u'Pz Espa\u00f1a')

    # TODO: add tests for GeoJSON sources
    # TODO: add tests for CSV sources
    # TODO: add test for lake-man-jp.json (CSV, Shift-JIS)
    # TODO: add test for lake-man-3740.json (CSV, not EPSG 4326)
    # TODO: add tests for encoding tags
    # TODO: add tests for SRS tags

    def test_lake_man_split2(self):
        rc, dest_path = self._run_conform_on_source('lake-man-split2', 'geojson')
        self.assertEqual(0, rc)

        with open(dest_path) as fp:
            rows = list(unicodecsv.DictReader(fp))
            self.assertEqual(rows[0]['NUMBER'], '1')
            self.assertEqual(rows[0]['STREET'], 'Spectrum Pointe Drive #320')
            self.assertEqual(rows[1]['NUMBER'], '')
            self.assertEqual(rows[1]['STREET'], '')
            self.assertEqual(rows[2]['NUMBER'], '300')
            self.assertEqual(rows[2]['STREET'], 'East Chapman Avenue')
            self.assertEqual(rows[3]['NUMBER'], '1')
            self.assertEqual(rows[3]['STREET'], 'Spectrum Pointe Drive #320')
            self.assertEqual(rows[4]['NUMBER'], '1')
            self.assertEqual(rows[4]['STREET'], 'Spectrum Pointe Drive #320')
            self.assertEqual(rows[5]['NUMBER'], '1')
            self.assertEqual(rows[5]['STREET'], 'Spectrum Pointe Drive #320')

class TestConformMisc(unittest.TestCase):
    def test_find_shapefile_source_path(self):
        shp_conform = {"conform": { "type": "shapefile" } }
        self.assertEqual("foo.shp", find_source_path(shp_conform, ["foo.shp"]))
        self.assertEqual("FOO.SHP", find_source_path(shp_conform, ["FOO.SHP"]))
        self.assertEqual("xyzzy/FOO.SHP", find_source_path(shp_conform, ["xyzzy/FOO.SHP"]))
        self.assertEqual("foo.shp", find_source_path(shp_conform, ["foo.shp", "foo.prj", "foo.shx"]))
        self.assertEqual(None, find_source_path(shp_conform, ["nope.txt"]))
        self.assertEqual(None, find_source_path(shp_conform, ["foo.shp", "bar.shp"]))

        shp_file_conform = {"conform": { "type": "shapefile", "file": "foo.shp" } }
        self.assertEqual("foo.shp", find_source_path(shp_file_conform, ["foo.shp"]))
        self.assertEqual("foo.shp", find_source_path(shp_file_conform, ["foo.shp", "bar.shp"]))
        self.assertEqual("xyzzy/foo.shp", find_source_path(shp_file_conform, ["xyzzy/foo.shp", "xyzzy/bar.shp"]))

        shp_poly_conform = {"conform": { "type": "shapefile-polygon" } }
        self.assertEqual("foo.shp", find_source_path(shp_poly_conform, ["foo.shp"]))

        broken_conform = {"conform": { "type": "broken" }}
        self.assertEqual(None, find_source_path(broken_conform, ["foo.shp"]))

    def test_find_geojson_source_path(self):
        geojson_conform = {"conform": {"type": "geojson"}}
        self.assertEqual("foo.json", find_source_path(geojson_conform, ["foo.json"]))
        self.assertEqual("FOO.JSON", find_source_path(geojson_conform, ["FOO.JSON"]))
        self.assertEqual("xyzzy/FOO.JSON", find_source_path(geojson_conform, ["xyzzy/FOO.JSON"]))
        self.assertEqual("foo.json", find_source_path(geojson_conform, ["foo.json", "foo.prj", "foo.shx"]))
        self.assertEqual(None, find_source_path(geojson_conform, ["nope.txt"]))
        self.assertEqual(None, find_source_path(geojson_conform, ["foo.json", "bar.json"]))

    def test_find_csv_source_path(self):
        csv_conform = {"conform": {"type": "csv"}}
        self.assertEqual("foo.csv", find_source_path(csv_conform, ["foo.csv"]))

class TestConformCsv(unittest.TestCase):
    "Fixture to create real files to test csv_source_to_csv()"

    # Test strings. an ASCII CSV file (with 1 row) and a Unicode CSV file,
    # along with expected outputs. These are Unicode strings; test code needs
    # to convert the input to bytes with the tested encoding.
    _ascii_header_in = u'STREETNAME,NUMBER,LATITUDE,LONGITUDE'
    _ascii_row_in = u'MAPLE ST,123,39.3,-121.2'
    _ascii_header_out = u'STREETNAME,NUMBER,X,Y'
    _ascii_row_out = u'MAPLE ST,123,-121.2,39.3'
    _unicode_header_in = u'STRE\u00c9TNAME,NUMBER,\u7def\u5ea6,LONGITUDE'
    _unicode_row_in = u'\u2603 ST,123,39.3,-121.2'
    _unicode_header_out = u'STRE\u00c9TNAME,NUMBER,X,Y'
    _unicode_row_out = u'\u2603 ST,123,-121.2,39.3'

    def setUp(self):
        self.testdir = tempfile.mkdtemp(prefix='openaddr-testPyConformCsv-')

    def tearDown(self):
        shutil.rmtree(self.testdir)

    def _convert(self, conform, src_bytes):
        "Convert a CSV source (list of byte strings) and return output as a list of unicode strings"
        assert not isinstance(src_bytes, unicode)
        src_path = os.path.join(self.testdir, "input.csv")
        open(src_path, "w+b").write('\n'.join(src_bytes))

        dest_path = os.path.join(self.testdir, "output.csv")
        csv_source_to_csv(conform, src_path, dest_path)
        return [s.decode('utf-8').strip() for s in open(dest_path, 'rb')]

    def test_simple(self):
        c = { "conform": { "type": "csv", "lat": "LATITUDE", "lon": "LONGITUDE" } }
        d = (self._ascii_header_in.encode('ascii'),
             self._ascii_row_in.encode('ascii'))
        r = self._convert(c, d)
        self.assertEqual(self._ascii_header_out, r[0])
        self.assertEqual(self._ascii_row_out, r[1])

    def test_utf8(self):
        c = { "conform": { "type": "csv", "lat": u"\u7def\u5ea6", "lon": u"LONGITUDE" } }
        d = (self._unicode_header_in.encode('utf-8'),
             self._unicode_row_in.encode('utf-8'))
        r = self._convert(c, d)
        self.assertEqual(self._unicode_header_out, r[0])
        self.assertEqual(self._unicode_row_out, r[1])

    def test_csvsplit(self):
        c = { "conform": { "csvsplit": ";", "type": "csv", "lat": "LATITUDE", "lon": "LONGITUDE" } }
        d = (self._ascii_header_in.replace(',', ';').encode('ascii'),
             self._ascii_row_in.replace(',', ';').encode('ascii'))
        r = self._convert(c, d)
        self.assertEqual(self._ascii_header_out, r[0])
        self.assertEqual(self._ascii_row_out, r[1])

        # unicodecsv freaked out about unicode strings for delimiter
        unicode_conform = { "conform": { "csvsplit": u";", "type": "csv", "lat": "LATITUDE", "lon": "LONGITUDE" } }
        r = self._convert(unicode_conform, d)
        self.assertEqual(self._ascii_row_out, r[1])

    @unittest.skip("Not yet implemented")
    def test_csvencoded(self):
        c = { "conform": { "encoding": "utf-8", "type": "csv", "lat": "\u7def\u5ea6", "lon": "LONGITUDE" } }
        d = (u'STRE\u00c9TNAME,NUMBER,\u7def\u5ea6,LONGITUDE'.encode('utf-8'),
             u'\u2603 ST,123,39.3,-121.2'.encode('utf-8'))
        r = self._convert(c, d)
        self.assertEqual(u'STRE\u00c9TNAME,NUMBER,\u7def\u5ea6,LONGITUDE', r[0])
