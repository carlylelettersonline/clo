import re
import json
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from natsort import natsorted
from corpus import Job


REGISTRY = {
    "Import CLO Data from TEI Repo": {
        "version": "0.1",
        "jobsite_type": "HUEY",
        "track_provenance": True,
        "create_report": True,
        "content_type": "Corpus",
        "configuration": {
            "parameters": {
                "tei_repo": {
                    "value": "",
                    "type": "corpus_repo",
                    "label": "CLO TEI Repository",
                    "note": "Likely named clo_tei"
                }
            },
        },
        "module": 'plugins.clo.tasks',
        "functions": ['ingest_data']
    },
}


ref_target_matcher = re.compile(r'volume-([^\/]*)\/(.*)')


def ingest_data(job_id):
    job = Job(job_id)
    corpus = job.corpus
    tei_repo_name = job.get_param_value('tei_repo')
    tei_repo = corpus.repos[tei_repo_name]

    job.set_status('running')
    job.report("Pulling down latest commits to TEI repo...")
    tei_repo.pull(corpus)

    job.report("Deleting stale data...")
    delete_clo_data(corpus)

    tei_path = tei_repo.path

    volume_files = [f for f in os.listdir(tei_path) if f.lower().startswith('volume') and f.lower().endswith('xml') and 'index' not in f]
    volume_files.sort()
    volume_id_map = {}
    interlocutor_id_map = {}
    volumes_needing_id_fix = []
    front_matter_errors = []
    letter_errors = []
    
    for volume_file in volume_files:
        job.report(f"Parsing {volume_file}.")
        volume_file = f'{tei_path}/{volume_file}'
        with open(volume_file, 'r') as tei_in:
            tei_text = tei_in.read()
    
        tei = BeautifulSoup(tei_text, 'xml')
    
        # determine volume number
        vol_no = None
    
        text_tag = tei.find('text')
        if 'id' in text_tag.attrs:
            vol_no = text_tag['id']
        elif 'xml:id' in text_tag.attrs:
            vol_no = text_tag['xml:id']
        else:
            volumes_needing_id_fix.append(volume_file)
            vol_no = volume_file.replace(tei_path + '/', '').replace('-P5.xml', '')
    
        if vol_no:
            vol_no = int(vol_no.lower().replace('volume-', ''))
            volume = corpus.get_content('LetterVolume')
            volume.volume_no = vol_no
    
            # determine description
            volume.description = tei.find_all('publicationStmt')[1].find_all('p')[1].text.strip()
    
            # gather front matter
            fm_divs = tei.find_all('div1', attrs={'type': 'section'})
            for fm_div in fm_divs:
                fm = corpus.get_content('FrontMatter')
                fm_info = {'slug': front_matter_slug(fm_div['id'])}
                fm.html = parse_front_matter(fm_div, fm_info)
    
                if 'errors' in fm_info and fm_info['errors']:
                    front_matter_errors += fm_info['errors']
    
                if fm_info['title']:
                    fm.title = fm_info['title']
                fm.slug = fm_info['slug']
                fm.footnotes = fm_info.get('footnotes', [])
                fm.save()
                volume.front_matters.append(fm.id)
    
            # parse letters
            volume_letters = {}
            letter_divs = tei.find_all('div3', attrs={'type': 'letter'})
            for letter_div in letter_divs:
                letter = corpus.get_content('Letter')
                letter_info = {}
                letter.html = parse_letter(letter_div, letter_info)
    
                if 'errors' in letter_info and letter_info['errors']:
                    letter_errors += letter_info['errors']
    
                # catch malformed dates
                try:
                    letter.date = date_parser.parse(letter_info.get('date'))
                except:
                    letter.date = None
                    job.report(f'''Letter "{letter_info.get('doi')}" from volume {vol_no} has invalid date: {letter_info.get('date')}''')

                # handle sender/recipient
                sender = None
                recipient = None
                if 'sender' in letter_info:
                    sender = get_interlocutor_id(corpus, letter_info['sender'], interlocutor_id_map)

                if 'addressee' in letter_info:
                    recipient = get_interlocutor_id(corpus, letter_info['addressee'], interlocutor_id_map)

                letter.date_label = letter_info.get('date_label')
                letter.description = letter_info.get('description')
                letter.doi = letter_info.get('doi')
                letter.vol_no = vol_no
                letter.sender = sender
                letter.recipient = recipient
                letter.sourcenote = letter_info.get('sourcenote')
                letter.footnotes = letter_info.get('footnotes')
                letter.save()

                if letter.date:
                    letter_key = f'{letter.date.year}-{letter.date.month}-{letter.date.day}-{letter.id}'
                    volume_letters[letter_key] = letter.id
                elif vol_no == 0:
                    letter_key = letter.doi
                    volume_letters[letter_key] = letter.id
    
            sorted_letter_keys = natsorted(list(volume_letters.keys()))
            for sorted_letter_key in sorted_letter_keys:
                volume.letters.append(volume_letters[sorted_letter_key])

            volume.save()
            volume_id_map[vol_no] = volume.id
    
    job.report("Creating volume batches...")
    create_volume_batches(corpus, volume_id_map)

    job.report("Importing photos...")
    import_photos(job, corpus, tei_path, volume_id_map)

    job.report("Importing manuscript images...")
    import_manuscripts(corpus, tei_path)
    
    if front_matter_errors:
        front_matter_errors = sorted(list(set(front_matter_errors)))
        job.report("\nFront Matter Parsing Errors: \n{0}".format('\n\t'.join(front_matter_errors)))
    
    if letter_errors:
        letter_errors = sorted(list(set(letter_errors)))
        job.report("\nLetter Parsing Errors: \n{0}".format('\n\t'.join(letter_errors)))

    job.complete(status='complete')


def delete_clo_data(corpus):
    clo_cts = [
        'PhotoAlbum',
        'Photo',
        'VolumeBatch',
        'SpecialCollection',
        'LetterVolume',
        'FrontMatter',
        'Letter',
        'Interlocutor'
    ]

    for clo_ct in clo_cts:
        contents = corpus.get_content(clo_ct, all=True)
        for content in contents:
            content.delete(track_deletions=False)


def parse_front_matter(tag, info={}, parser=None):
    html = ""

    if 'title' not in info:
        info['title'] = ""
    if 'errors' not in info:
        info['errors'] = []

    ignore_tags = [
        'sourceNote'
    ]

    if tag.name:
        if tag.name in ignore_tags:
            pass

        elif tag.name == 'idno' and 'type' in tag.attrs and tag.attrs['type'] in ['firstpage', 'lastpage']:
            pass

        elif tag.name == 'title' and not info['title']:
            info['title'] = "".join([tei_to_html(child, info, parse_front_matter) for child in tag])
        else:
            html += tei_to_html(tag, info, parse_front_matter)
    else:
        html += tei_to_html(tag, info, parse_front_matter)

    return html


def parse_letter(tag, info={}, parser=None):
    html = ""

    if 'errors' not in info:
        info['errors'] = []

    ignore_tags = [
        'sic'
    ]

    if tag.name:
        if tag.name in ignore_tags:
            pass

        elif tag.name == 'bibl' and 'xml:id' in tag.attrs:
            info['doi'] = tag['xml:id']

            docDate = tag.find('docDate')
            if 'value' in docDate.attrs:
                info['date'] = docDate['value']
                if info['date'].endswith('00'):
                    info['date'] = info['date'].replace('-00', '-01')

            info['date_label'] = docDate.text

            persons = tag.find_all('person')
            for person in persons:
                if 'type' in person.attrs and person['type'] in ['sender', 'addressee']:
                    if person['type'] not in info:
                        info[person['type']] = person.text.strip()

        elif tag.name == 'head':
            info['description'] = tag.text.strip()

        elif tag.name == 'sourceNote':
            info['sourcenote'] = "".join([parse_letter(child, info, parse_letter) for child in tag])

        else:
            html += tei_to_html(tag, info, parse_letter)
    else:
        html += tei_to_html(tag, info, parse_letter)

    return html


def tei_to_html(tag, info, parser):
    html = ""

    simple_conversions = {
        'hi': 'span',
        'bold': 'b',
        'l': 'div:verse-line',
        'opener': 'div:opener',
        'dateline': 'div:dateline',
        'date': 'span:date',
        'salute': 'span:salutation',
        'p': 'p',
        'lb': 'br/',
        'br': 'br/',
        'unclear': 'span:unclear',
        'del': 'span:deletion',
        'add': 'span:addition',
        'closer': 'div:closer',
        'postscript': 'div:postscript',
        'note': 'span:note',
        'address': 'span:address',
        'addrLine': 'br/',
        'quote': 'quote',
        'roleName': 'span:role',
        'abbr': 'span:abbreviation',
        'cell': 'td:m-2',
        'corr': 'span:correction',
        'lg': 'div:lg',
        'listBibl': 'p',
        'bibl': 'p',
        'q': 'p:ml-2',
        'row': 'tr:align-top',
        'table': 'table'
    }

    silent = [
        'body', 'div', 'orig', 'reg', 'title', 'name', 'forename', 'surname', 'pb', 'div1', 'foreign'
    ]

    discard = [
        'sic'
    ]

    if tag.name:
        if tag.name in silent:
            for child in tag.children:
                html += parser(child, info, parser)

        else:
            attributes = ""
            classes = []

            if 'rend' in tag.attrs:
                classes += ["rend-{0}".format(slugify(r)) for r in tag['rend'].split() if r]

            if tag.name == 'note':
                note_content = "".join([parser(child, info, parser) for child in tag])
                if 'footnotes' not in info:
                    info['footnotes'] = []
                info['footnotes'].append(note_content)
                note_number = len(info['footnotes'])
                html += f'<sup class="footnote origin" data-note_number="{note_number}">{note_number}</sup>'

            elif tag.name == 'ref' and 'target' in tag.attrs:
                match = ref_target_matcher.match(tag['target'])
                if match:
                    target_vol_no = match.group(1)
                    target_uri = match.group(2)
                    link_url = f"/volume/{target_vol_no}/{target_uri}"

                    if target_uri.startswith('http'):
                        link_url = target_uri
                    elif not target_uri.startswith('lt-'):
                        link_url = f"/volume/{target_vol_no}/{front_matter_slug(target_uri)}"

                    link_label = "".join([parser(child, info, parser) for child in tag])
                    html += f'<a href="{link_url}" target="_blank">{link_label}</a>'

            elif tag.name == 'head':
                html += '<p class="mb-2"><b>'
                html += "".join([parser(child, info, parser) for child in tag])
                html += '</b></p>'

            elif tag.name == 'figure':
                html += '<div class="clo-figure-div">'
                html += "".join([parser(child, info, parser) for child in tag])
                html += '</div>'

            elif tag.name == 'graphic' and 'url' in tag.attrs:
                html += f'<img class="clo-figure" src="https://iiif.dh.tamu.edu/iiif/2/CLO%2Ffigures%2F{tag["url"]}/full/full/0/default.jpg" />'

            elif tag.name == 'person' and 'reg' in tag.attrs:
                html += f'<span class="clo-person" title="{tag["reg"]}">'
                html += "".join([parser(child, info, parser) for child in tag])
                html += '</span>'

            elif tag.name == 'choice':
                original = tag.find('orig')
                if original:
                    original = original.get_text().strip().replace('"', '\"')
                else:
                    original = ""

                regularized = tag.find('reg')
                if regularized:
                    html += '''<span class="regularized" data-original="{0}">'''.format(original)
                    html += "".join([parser(child, info, parser) for child in regularized.children])
                    html += "</span>"

            elif tag.name in simple_conversions:
                html_tag = simple_conversions[tag.name]
                self_closing = html_tag.endswith('/')
                if self_closing:
                    html_tag = html_tag[:-1]

                if ':' in html_tag:
                    html_tag = html_tag.split(':')[0]
                    classes.append(simple_conversions[tag.name].split(':')[1])

                if classes:
                    attributes += ' class="{0}"'.format(" ".join(classes))
                    if self_closing:
                        attributes += ' /'

                html += "<{0}{1}>".format(
                    html_tag,
                    attributes
                )
                html += "".join([parser(child, info, parser) for child in tag])
                if not self_closing:
                    html += "</{0}>".format(html_tag)

            # tags to ignore (but keep content inside)
            elif tag.name in silent:
                html += "".join([parser(child, info, parser) for child in tag])

            # tags where we want to discard both tag and content
            elif tag.name in discard:
                pass

            else:
                info['errors'].append("Unhandled tag: {0}".format(log_tag(tag)))
                html += "".join([parser(child, info, parser) for child in tag])

    else:
        html += tag.get_text()

    return html


def get_interlocutor_id(corpus, name, interlocutor_id_map):
    if name not in interlocutor_id_map:
        interlocutor = corpus.get_content('Interlocutor')
        interlocutor.name = name
        interlocutor.save()
        interlocutor_id_map[name] = interlocutor.id

    return interlocutor_id_map[name]


def log_tag(tag):
    log = ""
    if tag.name:
        log = "[{0}]".format(tag.name)
        if tag.attrs:
            log += " {"
            for attr in tag.attrs.keys():
                log += " {0}={1}".format(attr, tag.attrs[attr])
            log += " }"
    return log


def create_volume_batches(corpus, volume_id_map):
    batches = [
        {
            'title': "The Carlyles in Scotland and in London",
            'date_range': "1812 to 1840",
            'volumes': [1, 12],
            'selected_contents': '''Ecclefechan / School Days / Edward Irving / Teacher and Tutor / Edinburgh / Courtship and Marriage / Craigenputtoch / Ralph Waldo Emerson / London / Leigh Hunt / John Stuart Mill / John Sterling / Robert Browning / Alfred Tennyson / Erasmus Darwin / Harriet Martineau / John Forster / Lectures on Literature, Revolution, and Heroes / Publication of Elements of Geometry (1822), <i>Wilhelm Meister</i> (1824), <i>Life of Schiller</i> (1825), <i>German Romance</i> (1827), “Burns” (1828), “Signs of the Times” (1829), “On History” (1830), “Characteristics” (1831), <i>Sartor Resartus</i> (1833–34), <i>French Revolution</i> (1837), <i>Essays</i> (1838), and <i>Chartism</i> (1839)''',
            'order': 1
        },
        {
            'title': "Success and Security",
            'date_range': "1841 to 1850",
            'volumes': [13, 25],
            'selected_contents': '''Samuel Laurence’s Portraits / Geraldine Jewsbury / Death of JWC’s Mother / Death of Sterling / William Makepeace Thackeray / Corn Laws / Charles Gavan Duffy / Journeys to Ireland and to Germany / International Copyright Law / John Ruskin / Lord and Lady Ashburton / Edward FitzGerald / Squire forgeries / R. M. Milnes / Giuseppe Mazzini / Emerson’s second visit / Joseph Neuberg / Louis Blanc / JWC’s screen / Margaret Fuller / John Tyndall / Publication of <i>Heroes and Hero-Worship</i> (1841), <i>Past and Present</i> (1843), <i>Oliver Cromwell’s Letters and Speeches</i> (1845), “The Negro Question” (1849), and <i>Latter-Day Pamphlets</i> (1850)''',
            'order': 2
        },
        {
            'title': "The “Valley of the Shadow of Frederick”",
            'date_range': "1851 to November 1862",
            'volumes': [26, 38],
            'selected_contents': '''Woolner’s Medallion / Crystal Palace / The Grange / Death of Wellington / Journeys to Paris and Germany / Sickness and Health / Garrett Study / John Ricardo / Holidays in Scotland / Death of TC’s Mother / American Investments / Crimean War / JWC’s “BUDGET of a Femme Incomprise” / “The Guises” / Alexander and Anne Gilchrist / Death of Lady Ashburton / Indian Mutiny / Tait’s A Chelsea Interior / Louisa Lady Ashburton / George Eliot / Ford Madox Ford’s <i>Work</i> / Death of Nero / Charlotte Cushman / American Civil War / Publication of <i>Life of Sterling</i> (1851), <i>Collected Works</i> (1857–58), and <i>Frederick</i>, Volumes 1–3 (1858, 1862)''',
            'order': 3
        },
        {
            'title': "“We shall go to them”",
            'date_range': "December 1862 to February 1881",
            'volumes': [39, 50],
            'selected_contents': '''Readings by Dickens / Moncure Conway / JWC’s Failing Health / Death of Thackeray / “Valley of the shadow of blue pill” / Lord Houghton / Margaret Oliphant / Death of Lord Ashburton / “Ilias (Americana) in Nuce” (1863) and Frederick, Volumes 4–6 (1864, 1865) / Death of JWC, 1866 / Eyre Defence Committee / Visit to Menton / Quarrels with Ruskin / Photos by Cameron / Shooting Niagara (1867) / the Watts portrait / the Library Edition / Mary Carlyle Aitken / Holidays with Lady Ashburton / Woolner’s plaster of TC’s hands / Cromwell’s death mask / Death of Dickens / the Franco-Prussian War / Farewell to Mazzini / John Ruskin / Emerson’s Last Visit''',
            'order': 4
        },
    ]

    for batch in batches:
        vb = corpus.get_content('VolumeBatch')
        vb.title = batch['title']
        vb.date_range = batch['date_range']
        vb.selected_contents = batch['selected_contents']
        vb.order = batch['order']

        for vol_no in range(batch['volumes'][0], batch['volumes'][1] + 1):
            if vol_no in volume_id_map:
                vb.volumes.append(volume_id_map[vol_no])

        vb.save()


def import_photos(job, corpus, tei_path, volume_id_map):
    album_files = [f for f in os.listdir(tei_path) if f.lower().startswith('album') and f.lower().endswith('xml')]

    for album_file in album_files:
        album_no = album_file.replace('album_', '').replace('.xml', '')
        if album_no.isdigit():
            album_no = int(album_no)
        else:
            job.report(f"{album_file} is not named according to photo album TEI file naming convention. Skipping...")
            continue

        album_file = f'{tei_path}/{album_file}'
        with open(album_file, 'r') as tei_in:
            tei_text = tei_in.read()

        tei = BeautifulSoup(tei_text, 'xml')

        album = corpus.get_content('PhotoAlbum')
        album.album_no = album_no

        # album title
        title_tag = tei.find('titlePart', attrs={'type': 'main'})
        if title_tag:
            album.title = title_tag.text.strip()
        else:
            job.report(f"Unable to determine title for album {album_file.replace(tei_path, '')}!")

        # album desc
        album_desc_div = tei.find('div', attrs={'type': 'description'})
        if album_desc_div and hasattr(album_desc_div, 'p'):
            album.description = album_desc_div.p.text.strip()
        else:
            job.report(f"Unable to determine description for album {album_file.replace(tei_path, '')}!")

        # get photos
        photo_tags = tei.find_all('div', attrs={'type': 'photo'})
        current_photo = 0
        for photo_tag in photo_tags:
            photo = corpus.get_content('Photo')
            photo.iiif_url = photo_tag.figure.graphic['url'].strip()
            photo.title = photo_tag.figure.head.text.strip()
            photo.description = photo_tag.figure.caption.text.strip()
            photo.date_taken = photo_tag.figure.note.date.text

            subjects = photo_tag.find('div', attrs={'type': 'subjects'}).find_all('p')
            for subject in subjects:
                photo.subjects.append(subject.text.strip())

            creators = photo_tag.figure.listPerson.find_all('persName')
            for creator in creators:
                photo.creators.append(creator.text.strip())

            photo.media_type = photo_tag.find('div', attrs={'type': 'mediaType'}).p.text.strip()
            photo.note = photo_tag.find('div', attrs={'type': 'note'}).p.text.strip()
            photo.source = photo_tag.find('div', attrs={'type': 'source'}).p.text.strip()
            photo.digital_specs = photo_tag.find('div', attrs={'type': 'digSpec'}).p.text.strip()
            photo.rights = photo_tag.find('div', attrs={'type': 'rights'}).p.text.strip()
            photo.language_note = photo_tag.find('div', attrs={'type': 'langNote'}).p.text.strip()
            photo.format = photo_tag.find('div', attrs={'type': 'format'}).p.text.strip()
            photo.publisher = photo_tag.find('div', attrs={'type': 'publisher'}).p.text.strip()

            if album_file.endswith('_0.xml'):
                relevant_volume_id = volume_id_map[current_photo]
                photo.frontispiece_volume = relevant_volume_id

            photo.save()
            album.photos.append(photo.id)
            current_photo += 1

        album.save()


def import_manuscripts(corpus, tei_path):
    iiif_base = "https://iiif.dh.tamu.edu/iiif/2/CLO%2Fmanuscripts%2F"
    manuscript_json_path = tei_path + '/manuscripts.json'
    with open(manuscript_json_path, 'r', encoding='utf-8') as manuscripts_in:
        letter_images = json.load(manuscripts_in)

    for letter_doi in letter_images.keys():
        letter = corpus.get_content('Letter', {'doi': letter_doi}, single_result=True)

        if letter:
            letter.page_images = []

            for letter_image in letter_images[letter_doi]:
                letter_image = letter_image.replace('/', '%2F')
                letter.page_images.append(f'{iiif_base}{letter_image}')

            letter.save()


def front_matter_slug(xml_id):
    slug = xml_id

    conversions = {
        'letters-to': 'letters_to_carlyles',
        'key-to': 'key_to_references',
        'Rival-Brothers': 'rival_brothers',
        'biographical': 'biographicalNotes',
        'in-memoriam': 'inMemoriam',
        'JWC-by-Robert': 'JWCbyTait',
        'TC-by-Robert': 'TCbyTait',
        'carlyle-notebook': 'janeNotebook',
        'carlyle-journal': 'janeJournal',
        'jane': 'janeJournal',
        'simple-story': 'simpleStory',
        'geraldine': 'geraldineJewsbury',
        'ellen-twisleton': 'ellenTwisleton',
        'athanaeum': 'athanaeumAdvertisements',
        'aurora-leigh': 'auroraComments',
        'will-of-TC': 'will_of_TC',
        'introduction': 'introduction',
        'acknowledgements': 'acknowledgements',
        'chronology': 'chronology',
        'comments': 'auroraComments',
        'references': 'key_to_references',
        'appendix': 'appendix',
    }

    for fragment, fixed_slug in conversions.items():
        if fragment in xml_id:
            slug = fixed_slug
            break
        
    return slug


# for posterity!
def create_album_tei(corpus):
    album_meta = [
        {
            'title': "Frontispieces of the <i>Duke-Edinburgh Edition</i>",
            'desc': "This is a special album collecting all of the images from the entire run to date of the Duke-Edinburgh Edition of the Carlyle Letters, including all frontispieces and all internal images."
        },
        {
            'title': "Album One",
            'desc': "“Tales of the Sun” / photographs by Robert Scott Tait (RST) assembled and bound into a presentation album by Geraldine Jewsbury (GEJ) / this volume with its laid in addenda includes 39 images / the title page is dated 1855, but numerous images are from two years later",
        },
        {
            'title': "Album Two",
            'desc': "Captioned by TC / “ These things I mark, mournfully, as a kind of duty,—this evg Monday 7 Octr 1867—T.C. ” / this album with its laid-in addenda includes 52 images",
        },
        {
            'title': "Album Three",
            'desc': "Captioned by TC / “This seems to have been gathered mainly at Haddington (in perhaps 1859 &c): I know few of the figures; mournfully mark this I do (Monday night, 7 Octr 1869) T.C.” / “x” in TC’s hand indicates that he was unable to identify the figure) / this album includes 44 images",
        },
        {
            'title': "Album Four",
            'desc': "Almost certainly collected and arranged by JWC / many images annotated by TC / this album includes 105 images",
        },
        {
            'title': "Album Five",
            'desc': "An assorted array, many pictures from TC and JWC’s era including material possibly removed from their portrait screens, other portraits and views collected, assembled, and arranged by Alexander Carlyle / this album contains 38 images",
        },
        {
            'title': "Album Six",
            'desc': "“Personal to Alexander Carlyle, T.C.’s nephew. Probably done after T.C.’s death.” / the album generally consists of two slots per page; some slots are blank and thus marked, in square brackets / this album contains 84 images",
        },
        {
            'title': "Album Seven",
            'desc': "“Personal to Alexander Carlyle, T.C.’s nephew. Probably done after T.C’s death.” / this album contains 140 images",
        },
    ]

    with open(corpus.path + '/files/photos.json', 'r', encoding='utf-8') as albums_in:
        albums = json.load(albums_in)

    for album in albums:
        album_no_parts = [p for p in album['imagesFolder'].split('/') if p]
        album_str = album_no_parts[-1].replace('album_', '')
        album_no = int(album_str)
        meta = album_meta[album_no]

        with open(f'{corpus.path}/files/album_{album_no}.xml', 'w', encoding='utf-8') as album_out:
            album_out.write(f'''<?xml version="1.0" encoding="UTF-8"?>
    <?xml-model href="http://www.tei-c.org/release/xml/tei/custom/schema/relaxng/tei_all.rng" type="application/xml" schematypens="http://relaxng.org/ns/structure/1.0"?>
    <?xml-model href="http://www.tei-c.org/release/xml/tei/custom/schema/relaxng/tei_all.rng" type="application/xml"
        schematypens="http://purl.oclc.org/dsdl/schematron"?>
    <TEI xmlns="http://www.tei-c.org/ns/1.0">
      <teiHeader>
          <fileDesc>
             <titleStmt>
                <title>Thomas Carlyle Photograph Albums, {meta['title']}</title>
             </titleStmt>
             <publicationStmt>
                <p>Alternative title is Tales of the Sun, 1855</p>
                <p>Electronic reproduction. New York, N.Y.: Columbia University Libraries, 2018. </p>
             </publicationStmt>
             <sourceDesc>
                <p>Original album forms part of the Thomas Carlyle Papers Collection at Columbia University Rare Book and Manuscript Library</p>
             </sourceDesc>
          </fileDesc>
      </teiHeader>
      <text id="{album_no}">
         <front>
            <titlePart type="main">{meta['title']}</titlePart>
            <div type="description">
               <p>{meta['desc']}</p>
            </div>
         </front>
          <body>
             <head>Thomas Carlyle Photograph Albums, Volume {album_no}</head>''')

            for photo_no in range(0, len(album['images'])):
                photo = album['images'][photo_no]
                creators = []
                for creator in photo['metadata']['creators']:
                    creators.append(f'''
                      <person>
                         <persName>{creator}</persName>
                      </person>''')

                subjects = []
                for subject in photo['metadata']['subjects']:
                    subjects.append(f'''
                    <p>{subject}</p>''')

                album_out.write(f'''
             <div type="photo" id="{photo_no}">
                <figure>
                   <head>{photo['metadata']['title']}</head>
                   <graphic url="https://iiif.dh.tamu.edu/iiif/2/CLO%2Falbum_{album_str}%2F{photo['imageUrl'].replace('.gif', '.jpg')}"/>
                   <caption>{photo['metadata']['description']}</caption>
                   <note>
                      <date when="{photo['metadata']['date']}">{photo['metadata']['date']}</date>
                   </note>
                   <listPerson type="creator">{''.join(creators)}
                   </listPerson>
                   <div type="subjects">{''.join(subjects)}
                   </div>
                   <div type="mediaType">
                      <p>{photo['metadata']['media_type']}</p>
                   </div>
                   <div type="note">
                      <p>{photo['metadata']['note']}</p>
                   </div>
                   <div type="source">
                      <p>{photo['metadata']['source']}</p>
                   </div>
                   <div type="digSpec"> 
                      <p>{photo['metadata']['digital_specs']}</p>
                   </div>
                   <div type="rights">
                      <p>{photo['metadata']['rights']}</p>
                   </div>
                   <div type="langNote">
                      <p>{photo['metadata']['language_note']}</p>
                   </div>
                   <div type="format">
                      <p>{photo['metadata']['format']}</p>
                   </div>
                   <div type="publisher">
                      <p>{photo['metadata']['publisher']}</p>
                   </div>
                </figure>
             </div>''')

            album_out.write('''
            </body>
         </text>
       </TEI>
            ''')
