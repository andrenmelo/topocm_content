#!/usr/bin/env python3

import argparse
import datetime
import io
from itertools import dropwhile
import os
import re
import tarfile
import tempfile
from time import strptime
from types import SimpleNamespace
import shutil
import urllib.request
from xml.etree.ElementTree import SubElement
from xml.etree import ElementTree

import nbformat
from nbformat import v4 as current
from nbconvert import HTMLExporter
from traitlets.config import Config

try:
    os.environ['PYTHONPATH'] = os.environ['PYTHONPATH'] + ':./code'
except KeyError:
    os.environ['PYTHONPATH'] = './code'

scripts_path = os.path.dirname(os.path.realpath(__file__))
mooc_folder = os.path.join(scripts_path, os.pardir)

cfg = Config({
    'HTMLExporter': {
        'template_file': 'edx',
        'template_path': ['.', scripts_path],
        'exclude_input': True,
    }
})
exportHtml = HTMLExporter(config=cfg)

url = (
    "https://cdnjs.cloudflare.com/ajax/libs"
    "/iframe-resizer/3.5.14/iframeResizer.min.js"
)
js = urllib.request.urlopen(url).read().decode('utf-8')

IFRAME_TEMPLATE = r"""
<iframe id="{id}" scrolling="no" width="100%" frameborder=0>
Your browser does not support IFrames.
</iframe>

<script>
var iframe = document.getElementById('{id}');
iframe.src =  "//" +
              (document.domain.endsWith("edge.edx.org") ? "test." : "") +
              "topocondmat.org/edx/{id}.html?date=" + (+ new Date());
</script>

<script>{js}</script>

<script>
if (require === undefined) {{
// Detect IE10 and below
var isOldIE = (navigator.userAgent.indexOf("MSIE") !== -1);
iFrameResize({{
    heightCalculationMethod: isOldIE ? 'max' : 'lowestElement',
    minSize:100,
    log:true,
    checkOrigin:false
    }}, "#{id}");
}} else {{
  require(["{url}"], (iFrameResize) => iFrameResize())
}}
</script>
"""

with open(os.path.join(scripts_path, 'release_dates')) as f:
    release_dates = eval(f.read())


def date_to_edx(date, add_days=0):
    tmp = strptime(date, '%d %b %Y')

    date = datetime.datetime(tmp.tm_year, tmp.tm_mon, tmp.tm_mday, 10)
    date = date + datetime.timedelta(days=add_days)
    date = date.strftime('%Y-%m-%dT%H:%M:%SZ')
    return date


def parse_syllabus(syllabus_file, content_folder=''):
    # loading raw syllabus
    syll = nbformat.read(syllabus_file, as_version=4).cells[0].source

    section = '^\* \*\*(?P<section>.*)\*\*$'
    subsection = '^  \* \[(?P<title>.*)\]\((?P<filename>.*)\)$'
    syllabus_line = section + '|' + subsection

    syllabus = []
    for line in syll.split('\n'):
        match = re.match(syllabus_line, line)
        if match is None:
            continue
        name = match.group('section')
        if name is not None:
            syllabus.append([name, release_dates.get(name), []])
            continue
        name, filename = match.group('title'), match.group('filename')
        syllabus[-1][-1].append((name, filename))

    data = SimpleNamespace(category='main', chapters=[])
    for i, section in enumerate(syllabus):
        if section[1] is None:
            continue

        # creating chapter
        chapter = SimpleNamespace(category='chapter', sequentials=[])

        chapter.name = section[0]
        chapter.date = section[1]
        chapter.url = f"sec_{i:02}"

        for j, subsection in enumerate(section[2]):
            # creating sequential
            sequential = SimpleNamespace(category='sequential', verticals=[])

            sequential.name = subsection[0]
            sequential.date = chapter.date
            sequential.url = f"subsec_{i:02}_{j:02}"
            sequential.source_notebook = content_folder + '/' + subsection[1]

            chapter.sequentials.append(sequential)

        data.chapters.append(chapter)
    return data


def split_into_units(nb_name):
    """Split notebook into units where top level headings occur."""
    nb = nbformat.read(nb_name, as_version=4)

    # Split markdown cells on titles.
    def split_cells():
        cells = dropwhile(
            (lambda cell: cell.cell_type != 'markdown'),
            nb.cells
        )
        for cell in cells:
            if cell.cell_type != 'markdown':
                yield cell
            else:
                split_sources = re.split(
                    '(^# .*$)', cell.source, flags=re.MULTILINE
                )
                for src in split_sources:
                    yield nbformat.NotebookNode(
                        source=src,
                        cell_type='markdown',
                        metadata={},
                    )

    units = []
    for cell in split_cells():
        if cell.cell_type == 'markdown' and cell.source.startswith('# '):
            nb_name = re.match('^# (.*)$', cell.source).group(1)
            units.append(current.new_notebook(metadata={
                'name': nb_name
            }))
        else:
            if not units:  # We did not encounter a title yet.
                continue
            units[-1].cells.append(cell)

    return units


def convert_normal_cells(normal_cells):
    """ Convert normal_cells into html. """
    for cell in normal_cells:
        if cell.cell_type == 'markdown':
            cell.source = re.sub(r'\\begin\{ *equation *\}', '\[', cell.source)
            cell.source = re.sub(r'\\end\{ *equation *\}', '\]', cell.source)
    tmp = current.new_notebook(cells=normal_cells)
    html = exportHtml.from_notebook_node(tmp)[0]
    return html


def convert_unit(unit, date):
    """ Convert unit into html and special xml componenets. """
    cells = unit.cells

    unit_output = []
    normal_cells = []

    for cell in cells:
        # Markdown-like cell
        if cell.cell_type == 'markdown':
            normal_cells.append(cell)
            continue

        # Empty code cell
        if not hasattr(cell, 'outputs'):
            continue

        xml_components = []
        for output in cell.outputs:
            data = output.get('data')
            if data and 'application/vnd.edx.olxml+xml' in data:
                xml_components.append(
                    data['application/vnd.edx.olxml+xml']
                )

        # Regular code cell
        if not xml_components:
            normal_cells.append(cell)
            continue

        if len(xml_components) > 1:
            raise RuntimeError('More than 1 xml component in a cell.')

        # Cells with mooc components, special processing required
        xml = ElementTree.fromstring(xml_components[0])

        if normal_cells:
            html = convert_normal_cells(normal_cells)
            unit_output.append(html)
            normal_cells = []
        unit_output.append(xml)

    if normal_cells:
        html = convert_normal_cells(normal_cells)
        unit_output.append(html)
        normal_cells = []

    return unit_output


def converter(mooc_folder, args, content_folder=None):
    """ Do converting job. """
    # Mooc content location
    if content_folder is None:
        content_folder = mooc_folder

    # copying figures
    target = os.path.join(mooc_folder, 'generated')
    os.makedirs(os.path.join(target, 'html/edx/figures'), exist_ok=True)
    for entry, *_ in os.walk(content_folder):
        if re.match(content_folder + r'/w\d+_.+/figures', entry):
            for filename in os.listdir(entry):
                shutil.copy(os.path.join(entry, filename),
                            os.path.join(target, 'html/edx/figures'))
    html_folder = os.path.join(target, 'html/edx')

    # Temporary locations
    dirpath = tempfile.mkdtemp() + '/course'

    skeleton = mooc_folder + '/edx_skeleton'
    shutil.copytree(skeleton, dirpath)

    # Loading data from syllabus
    syllabus_nb = os.path.join(content_folder, 'syllabus.ipynb')
    data = parse_syllabus(syllabus_nb, content_folder)

    course_xml_path = os.path.join(dirpath, 'course.xml')
    with open(course_xml_path) as f:
        xml_course = ElementTree.fromstring(f.read())

    for chapter in data.chapters:
        chapter_xml = SubElement(xml_course, 'chapter', attrib=dict(
            url_name=chapter.url,
            display_name=chapter.name,
            start=date_to_edx(chapter.date),
        ))

        for sequential in chapter.sequentials:
            sequential_xml = SubElement(chapter_xml, 'sequential', attrib=dict(
                url_name=sequential.url,
                display_name=sequential.name,
                graded=('true' if chapter.url != 'sec_00' else 'false'),
            ))

            if sequential.name == 'Assignments':
                sequential_xml.attrib['format'] = "Research"
            elif chapter.url != 'sec_00':
                sequential_xml.attrib['format'] = "Self-check"

            units = split_into_units(sequential.source_notebook)

            for i, unit in enumerate(units):
                vertical_url = sequential.url + f'_{i:02}'
                # add vertical info to sequential_xml
                vertical = SubElement(sequential_xml, 'vertical', attrib=dict(
                    url_name=vertical_url,
                    display_name=unit.metadata.name,
                ))

                unit_output = convert_unit(unit, date=sequential.date)
                for (j, out) in enumerate(unit_output):
                    out_url = vertical_url + f"_out_{j:02}"
                    if isinstance(out, str):
                        # adding html subelement
                        SubElement(vertical, 'html', attrib=dict(
                            url_name=out_url,
                            display_name=unit.metadata.name,
                            filename=out_url
                        ))

                        html_path = os.path.join(html_folder,
                                                 out_url + '.html')
                        with open(html_path, 'w') as f:
                            f.write(out)

                        html_path = os.path.join(dirpath, 'html',
                                                 out_url + '.html')
                        with open(html_path, 'w') as f:
                            f.write(IFRAME_TEMPLATE.format(
                                id=out_url, url=url, js=js
                            ))

                    else:
                        # adding video subelement
                        vertical.append(out)
                        if 'url_name' not in out.attrib:
                            out.attrib['url_name'] = out_url

    with open(course_xml_path, 'w') as f:
        f.write(ElementTree.tostring(xml_course, encoding='unicode'))

    # Creating tar
    tar_filepath = os.path.join(target, 'import_to_edx.tar.gz')
    tar = tarfile.open(name=tar_filepath, mode='w:gz')
    tar.add(dirpath, arcname='')
    tar.close()

    # Cleaning
    shutil.rmtree(dirpath)


def main():
    mooc_folder = os.path.join(os.path.dirname(__file__), os.path.pardir)
    parser = argparse.ArgumentParser()
    parser.add_argument('source', nargs='?', help='folder to convert')
    args = parser.parse_args()

    print('Path to mooc folder:', mooc_folder)
    print('Path to notebooks:', args.source)
    converter(mooc_folder, args, content_folder=args.source)


if __name__ == "__main__":
    main()
