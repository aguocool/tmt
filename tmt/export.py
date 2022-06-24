# coding: utf-8

""" Export metadata into nitrate """


import email.utils
import os
import re
import traceback
import types
import xmlrpc.client
from functools import lru_cache
from typing import Any, Dict, Generator, List, Optional, Tuple, Union, cast

import fmf
from click import echo, style

import tmt
import tmt.utils
from tmt.utils import ConvertError, check_git_url, markdown_to_html

bugzilla: Optional[types.ModuleType] = None
gssapi: Optional[types.ModuleType] = None
nitrate: Optional[types.ModuleType] = None

DEFAULT_PRODUCT: Any = None

SectionsReturnType = Tuple[str, str, str, str]
HeadingsType = List[List[Union[int, str]]]
SectionsHeadingsType = Dict[str, HeadingsType]

log = fmf.utils.Logging('tmt').logger

WARNING = """
Test case has been migrated to git. Any changes made here might be overwritten.
See: https://tmt.readthedocs.io/en/latest/questions.html#nitrate-migration
""".lstrip()

# For linking bugs
BUGZILLA_XMLRPC_URL = "https://bugzilla.redhat.com/xmlrpc.cgi"
EXTERNAL_TRACKER_ID = 69  # ID of nitrate in RH's bugzilla
RE_BUGZILLA_URL = r'bugzilla.redhat.com/show_bug.cgi\?id=(\d+)'


def import_nitrate() -> Any:
    """ Conditionally import the nitrate module """
    # Need to import nitrate only when really needed. Otherwise we get
    # traceback when nitrate not installed or config file not available.
    # And we want to keep the core tmt package with minimal dependencies.
    try:
        global nitrate, DEFAULT_PRODUCT, gssapi
        import gssapi
        import nitrate
        assert nitrate
        DEFAULT_PRODUCT = nitrate.Product(name='RHEL Tests')
        return nitrate
    except ImportError:
        raise ConvertError(
            "Install tmt-test-convert to export tests to nitrate.")
    except nitrate.NitrateError as error:  # type: ignore
        raise ConvertError(error)


def import_bugzilla() -> None:
    """ Conditionally import the bugzilla module """
    try:
        global bugzilla
        import bugzilla
    except ImportError:
        raise ConvertError(
            "Install 'tmt-test-convert' to link test to the bugzilla.")


def _nitrate_find_fmf_testcases(test: 'tmt.Test') -> Generator[Any, None, None]:
    """
    Find all Nitrate test cases with the same fmf identifier

    All component general plans are explored for possible duplicates.
    """
    assert nitrate
    for component in test.component:
        try:
            for testcase in find_general_plan(component).testcases:
                struct_field = tmt.utils.StructuredField(testcase.notes)
                try:
                    fmf_id = tmt.utils.yaml_to_dict(struct_field.get('fmf'))
                    if fmf_id == test.fmf_id:
                        echo(style(
                            f"Existing test case '{testcase.identifier}' "
                            f"found for given fmf id.", fg='magenta'))
                        yield testcase
                except tmt.utils.StructuredFieldError:
                    pass
        except nitrate.NitrateError:
            pass


def convert_manual_to_nitrate(test_md: str) -> SectionsReturnType:
    """
    Convert Markdown document to html sections.

    These sections can be exported to nitrate.
    Expects: Markdown document as a file.
    Returns: tuple of (step, expect, setup, cleanup) sections
    as html strings.
    """

    sections_headings: SectionsHeadingsType = {
        heading: []
        for heading_list in list(tmt.base.SECTIONS_HEADINGS.values())
        for heading in heading_list
        }

    html = markdown_to_html(test_md)
    html_splitlines = html.splitlines()

    for key in sections_headings.keys():
        result: HeadingsType = []
        i = 0
        while html_splitlines:
            try:
                if re.search("^" + key + "$", html_splitlines[i]):
                    html_content = str()
                    if key.startswith('<h1>Test'):
                        html_content = html_splitlines[i].\
                            replace('<h1>', '<b>').\
                            replace('</h1>', '</b>')
                    for j in range(i + 1, len(html_splitlines)):
                        if re.search("^<h[1-4]>(.+?)</h[1-4]>$",
                                     html_splitlines[j]):
                            result.append([i, html_content])
                            i = j - 1
                            break
                        html_content += html_splitlines[j] + "\n"
                        # Check end of the file
                        if j + 1 == len(html_splitlines):
                            result.append([i, html_content])
            except IndexError:
                sections_headings[key] = result
                break
            i += 1
            if i >= len(html_splitlines):
                sections_headings[key] = result
                break

    def concatenate_headings_content(headings: Tuple[str, ...]) -> HeadingsType:
        content = list()
        for v in headings:
            content += sections_headings[v]
        return content

    def enumerate_content(content: HeadingsType) -> HeadingsType:
        # for sorting convert the index to integer, but keep whole list as list of strings
        content.sort(key=lambda a: int(a[0]))
        for c in range(len(content)):
            content[c][1] = f"<p>Step {c + 1}.</p>" + cast(str, content[c][1])
        return content

    sorted_test = sorted(concatenate_headings_content((
        '<h1>Test</h1>',
        '<h1>Test .*</h1>')))

    sorted_step = sorted(enumerate_content(concatenate_headings_content((
        '<h2>Step</h2>',
        '<h2>Test Step</h2>'))) + sorted_test)
    step = ''.join([f"{v[1]}" for v in sorted_step])

    sorted_expect = sorted(enumerate_content(concatenate_headings_content((
        '<h2>Expect</h2>',
        '<h2>Result</h2>',
        '<h2>Expected Result</h2>'))) + sorted_test)
    expect = ''.join([f"{v[1]}" for v in sorted_expect])

    def check_section_exists(text: str) -> str:
        try:
            return cast(str, sections_headings[text][0][1])
        except (IndexError, KeyError):
            return ''

    setup = check_section_exists('<h1>Setup</h1>')
    cleanup = check_section_exists('<h1>Cleanup</h1>')

    return step, expect, setup, cleanup


def bz_set_coverage(bz_instance: Any, bug_ids: List[int], case_id: int) -> None:
    """ Set coverage in Bugzilla """
    overall_pass = True
    no_email = 1  # Do not send emails about the change
    get_bz_dict = {
        'ids': bug_ids,
        'include_fields': ['id', 'external_bugs', 'flags']}
    bugs_data = bz_instance._proxy.Bug.get(get_bz_dict)
    for bug in bugs_data['bugs']:
        # Process flag (might fail for some types)
        bug_id = bug['id']
        if 'qe_test_coverage+' not in set(
                [x['name'] + x['status'] for x in bug['flags']]):
            try:
                bz_instance._proxy.Flag.update({
                    'ids': [bug_id],
                    'nomail': no_email,
                    'updates': [{
                        'name': 'qe_test_coverage',
                        'status': '+'
                        }]
                    })
            except xmlrpc.client.Fault as err:
                log.debug(f"Update flag failed: {err}")
                echo(style(
                    f"Failed to set qe_test_coverage+ flag for BZ#{bug_id}",
                    fg='red'))
        # Process external tracker - should succeed
        current = set([int(b['ext_bz_bug_id']) for b in bug['external_bugs']
                       if b['ext_bz_id'] == EXTERNAL_TRACKER_ID])
        if case_id not in current:
            query = {
                'bug_ids': [bug_id],
                'nomail': no_email,
                'external_bugs': [{
                    'ext_type_id': EXTERNAL_TRACKER_ID,
                    'ext_bz_bug_id': case_id,
                    'ext_description': '',
                    }]
                }
            try:
                bz_instance._proxy.ExternalBugs.add_external_bug(query)
            except Exception as err:
                log.debug(f"Link case failed: {err}")
                echo(style(f"Failed to link to BZ#{bug_id}", fg='red'))
                overall_pass = False
    if not overall_pass:
        raise ConvertError("Failed to link the case to bugs.")


def export_to_nitrate(test: 'tmt.Test') -> None:
    """ Export fmf metadata to nitrate test cases """
    import_nitrate()
    assert nitrate
    assert gssapi

    # Check command line options
    create = test.opt('create')
    general = test.opt('general')
    link_runs = test.opt('link_runs')
    duplicate = test.opt('duplicate')
    link_bugzilla = test.opt('bugzilla')
    dry_mode = test.opt('dry')

    if link_runs:
        general = True

    if link_bugzilla:
        import_bugzilla()
        assert bugzilla
        try:
            bz_instance = bugzilla.Bugzilla(url=BUGZILLA_XMLRPC_URL)
        except Exception as exc:
            log.debug(traceback.format_exc())
            raise ConvertError(
                "Couldn't initialize the Bugzilla client.", original=exc)
        if not bz_instance.logged_in:
            raise ConvertError(
                "Not logged to Bugzilla, check `man bugzilla` section "
                "'AUTHENTICATION CACHE AND API KEYS'.")

    # Check nitrate test case
    try:
        nitrate_id = test.node.get('extra-nitrate')[3:]
        nitrate_case = nitrate.TestCase(int(nitrate_id))
        nitrate_case.summary  # Make sure we connect to the server now
        echo(style(f"Test case '{nitrate_case.identifier}' found.", fg='blue'))
    except TypeError:
        # Create a new nitrate test case
        if create:
            nitrate_case = None
            # Check for existing Nitrate tests with the same fmf id
            if not duplicate:
                testcases = _nitrate_find_fmf_testcases(test)
                try:
                    # Select the first found testcase if any
                    nitrate_case = next(testcases)
                except StopIteration:
                    pass
            if not nitrate_case:
                # Summary for TCMS case
                extra_summary = prepare_extra_summary(test)
                if not dry_mode:
                    nitrate_case = create_nitrate_case(extra_summary)
                else:
                    echo(style(
                        f"Test case '{extra_summary}' created.", fg='blue'))
                test._metadata['extra-summary'] = extra_summary
        else:
            raise ConvertError(f"Nitrate test case id not found for {test}"
                               " (You can use --create option to enforce"
                               " creating testcases)")
    except (nitrate.NitrateError, gssapi.raw.misc.GSSError) as error:
        raise ConvertError(error)

    # Check if URL is accessible, to be able to reach from nitrate
    check_git_url(test.fmf_id['url'])

    # Summary
    try:
        summary = (test._metadata.get('extra-summary')
                   or test._metadata.get('extra-task')
                   or prepare_extra_summary(test))
    except ConvertError:
        summary = test.name
    if not dry_mode:
        nitrate_case.summary = summary
    echo(style('summary: ', fg='green') + summary)

    # Script
    if test.node.get('extra-task'):
        if not dry_mode:
            nitrate_case.script = test.node.get('extra-task')
        echo(style('script: ', fg='green') + test.node.get('extra-task'))

    # Components and General plan
    # First remove any components that are already there
    if not dry_mode:
        nitrate_case.components.clear()
    # Only these general plans should stay
    expected_general_plans = set()
    # Then add fmf ones
    if test.component:
        echo(style('components: ', fg='green') + ' '.join(test.component))
        for component in test.component:
            try:
                nitrate_component = nitrate.Component(
                    name=component, product=DEFAULT_PRODUCT.id)
                if not dry_mode:
                    nitrate_case.components.add(nitrate_component)
            except nitrate.xmlrpc_driver.NitrateError as error:
                log.debug(error)
                echo(style(
                    f"Failed to add component '{component}'.", fg='red'))
            if general:
                try:
                    general_plan = find_general_plan(component)
                    expected_general_plans.add(general_plan)
                    echo(style(
                        f"Linked to general plan '{general_plan}'.",
                        fg='magenta'))
                    if not dry_mode:
                        nitrate_case.testplans.add(general_plan)
                    if link_runs:
                        add_to_nitrate_runs(
                            nitrate_case, general_plan, test, dry_mode)
                except nitrate.NitrateError as error:
                    log.debug(error)
                    echo(style(
                        f"Failed to find general test plan for '{component}'.",
                        fg='red'))
    # Remove unexpected general plans
    if general and nitrate_case:
        # Remove also all general plans linked to testcase
        for nitrate_plan in [plan for plan in nitrate_case.testplans]:
            if (nitrate_plan.type.name == "General"
                    and nitrate_plan not in expected_general_plans):
                echo(style(
                    f"Removed general plan '{nitrate_plan}'.", fg='red'))
                if not dry_mode:
                    nitrate_case.testplans.remove(nitrate_plan)

    # Tags
    # Convert 'tier' attribute into a Tier tag
    if test.tier is not None:
        test.tag.append(f"Tier{test.tier}")
    # Add special fmf-export tag
    test.tag.append('fmf-export')
    if not dry_mode:
        nitrate_case.tags.clear()
        nitrate_case.tags.add([nitrate.Tag(tag) for tag in test.tag])
    echo(style('tags: ', fg='green') + ' '.join(set(test.tag)))

    # Default tester
    if test.contact:
        try:
            # Need to pick one value, so picking the first contact
            email_address = email.utils.parseaddr(test.contact[0])[1]
            nitrate_user = nitrate.User(email_address)
            nitrate_user._fetch()  # To check that user exists
            if not dry_mode:
                nitrate_case.tester = nitrate_user
            echo(style('default tester: ', fg='green') + email_address)
        except nitrate.NitrateError as error:
            log.debug(error)
            raise ConvertError(f"Nitrate issue: {error}")

    # Duration
    if not dry_mode:
        nitrate_case.time = test.duration
    echo(style('estimated time: ', fg='green') + test.duration)

    # Manual
    if not dry_mode:
        nitrate_case.automated = not test.manual
    echo(style('automated: ', fg='green') + ['auto', 'manual'][test.manual])

    # Status
    current_status = nitrate_case.status if nitrate_case else nitrate.CaseStatus('CONFIRMED')
    # Enable enabled tests
    if test.enabled:
        if not dry_mode:
            nitrate_case.status = nitrate.CaseStatus('CONFIRMED')
        echo(style('status: ', fg='green') + 'CONFIRMED')
    # Disable disabled tests which are CONFIRMED
    elif current_status == nitrate.CaseStatus('CONFIRMED'):
        if not dry_mode:
            nitrate_case.status = nitrate.CaseStatus('DISABLED')
        echo(style('status: ', fg='green') + 'DISABLED')
    # Keep disabled tests in their states
    else:
        echo(style('status: ', fg='green') + str(current_status))

    # Environment
    if test.environment:
        environment = ' '.join(tmt.utils.shell_variables(test.environment))
        if not dry_mode:
            nitrate_case.arguments = environment
        echo(style('arguments: ', fg='green') + environment)
    else:
        # FIXME unable clear to set empty arguments
        # (possibly error in xmlrpc, BZ#1805687)
        if not dry_mode:
            nitrate_case.arguments = ' '
        echo(style('arguments: ', fg='green') + "' '")

    # Structured Field
    struct_field = tmt.utils.StructuredField(
        nitrate_case.notes if nitrate_case else '')
    echo(style('Structured Field: ', fg='green'))

    # Mapping of structured field sections to fmf case attributes
    section_to_attr = {
        'description': test.summary,
        'purpose-file': test.description,
        'hardware': test.node.get('extra-hardware'),
        'pepa': test.node.get('extra-pepa'),
        }
    for section, attribute in section_to_attr.items():
        if attribute is None:
            try:
                struct_field.remove(section)
            except tmt.utils.StructuredFieldError:
                pass
        else:
            struct_field.set(section, attribute)
            echo(style(section + ': ', fg='green') + attribute.strip())

    # fmf identifer
    fmf_id = tmt.utils.dict_to_yaml(test.fmf_id)
    struct_field.set('fmf', fmf_id)
    echo(style('fmf id:\n', fg='green') + fmf_id.strip())

    # Warning
    if WARNING not in struct_field.header():
        struct_field.header(WARNING + struct_field.header())
        echo(style(
            'Add migration warning to the test case notes.', fg='green'))

    # Saving case.notes with edited StructField
    if not dry_mode:
        nitrate_case.notes = struct_field.save()

    # Export manual test instructions from *.md file to nitrate as html
    md_path = return_markdown_file()
    if os.path.exists(md_path):
        step, expect, setup, cleanup = convert_manual_to_nitrate(md_path)
        if not dry_mode:
            nitrate.User()._server.TestCase.store_text(
                nitrate_case.id, step, expect, setup, cleanup)
        echo(style("manual steps:", fg='green') + f" found in {md_path}")

    # Append id of newly created nitrate case to its file
    if not test.node.get('extra-nitrate'):
        echo(style("Append the nitrate test case id.", fg='green'))
        if not dry_mode:
            try:
                with test.node as data:
                    data["extra-nitrate"] = nitrate_case.identifier
            except AttributeError:
                # FIXME: Remove this deprecated code after fmf support
                # for storing modified data is released long enough
                file_path = test.node.sources[-1]
                try:
                    with open(file_path, encoding='utf-8', mode='a+') as file:
                        file.write(
                            f"extra-nitrate: {nitrate_case.identifier}\n")
                except IOError:
                    raise ConvertError(
                        "Unable to open '{0}'.".format(file_path))

    # List of bugs test verifies
    verifies_bug_ids = []
    for link in test.link:
        try:
            bug_id_search = re.search(RE_BUGZILLA_URL, link['verifies'])
            if not bug_id_search:
                log.debug(f"Did not find bugzila URL in {link['verifies']}")
                continue
            bug_id = int(bug_id_search.group(1))
            verifies_bug_ids.append(bug_id)
        except Exception as err:
            log.debug(err)

    # Add bugs to the Nitrate case
    if verifies_bug_ids:
        echo(style('Verifies bugs: ', fg='green') +
             ', '.join([f"BZ#{b}" for b in verifies_bug_ids]))
    for bug_id in verifies_bug_ids:
        if not dry_mode:
            nitrate_case.bugs.add(nitrate.Bug(bug=int(bug_id)))

    # Update nitrate test case
    if not dry_mode:
        nitrate_case.update()
        echo(style("Test case '{0}' successfully exported to nitrate.".format(
            nitrate_case.identifier), fg='magenta'))

    # Optionally link Bugzilla to Nitrate case
    if link_bugzilla and verifies_bug_ids:
        try:
            if not dry_mode:
                bz_set_coverage(
                    bz_instance, verifies_bug_ids, int(
                        nitrate_case.id))
            echo(style("Linked to Bugzilla: {}.".format(", ".join(
                [f"BZ#{bz_id}" for bz_id in verifies_bug_ids])), fg='magenta'))
        except Exception as err:
            raise ConvertError("Couldn't update bugs", original=err)


def add_to_nitrate_runs(
        nitrate_case: Any,
        general_plan: Any,
        test: 'tmt.Test',
        dry_mode: bool) -> None:
    """
    Add nitrate test case to all active runs under given general plan

    Go down plan tree from general plan, add case and case run to
    all open runs. Try to apply adjust.
    """
    assert nitrate
    for child_plan in nitrate.TestPlan.search(parent=general_plan.id):
        for testrun in child_plan.testruns:
            if testrun.status == nitrate.RunStatus("FINISHED"):
                continue
            if not enabled_for_environment(test, tcms_notes=testrun.notes):
                continue
            # nitrate_case is None when --dry and --create are used together
            if not nitrate_case or child_plan not in nitrate_case.testplans:
                echo(style(f"Link to plan '{child_plan}'.", fg='magenta'))
                if not dry_mode:
                    nitrate_case.testplans.add(child_plan)
            if not nitrate_case or nitrate_case not in [
                    caserun.testcase for caserun in testrun]:
                echo(style(f"Link to run '{testrun}'.", fg='magenta'))
                if not dry_mode:
                    nitrate.CaseRun(testcase=nitrate_case, testrun=testrun)


def enabled_for_environment(test: 'tmt.Test', tcms_notes: str) -> bool:
    """ Check whether test is enabled for specified environment """
    field = tmt.utils.StructuredField(tcms_notes)
    context_dict = {}
    try:
        for line in cast(str, field.get('environment')).split('\n'):
            try:
                dimension, values = line.split('=', maxsplit=2)
                context_dict[dimension.strip()] = [
                    value.strip() for value in re.split(",|and", values)]
            except ValueError:
                pass
    except tmt.utils.StructuredFieldError:
        pass

    if not context_dict:
        return True

    try:
        context = fmf.context.Context(**context_dict)
        test_node = test.node.copy()
        test_node.adjust(context)
        # TODO: remove cast later
        return cast(bool, tmt.Test(test_node).enabled)
    except BaseException as exception:
        log.debug(f"Failed to process adjust: {exception}")
        return True


def check_md_file_respects_spec(md_path: str) -> List[str]:
    """
    Check that the file respects manual test specification

    Return list of warnings, empty list if no problems found.
    """
    warnings_list = []
    # TODO: remove cast lastr
    sections_headings = cast(Dict[str, List[str]], tmt.base.SECTIONS_HEADINGS)
    required_headings = set(sections_headings['Step'] +
                            sections_headings['Expect'])
    values = []
    for _ in list(sections_headings.values()):
        values += _

    md_to_html = tmt.utils.markdown_to_html(md_path)
    html_headings_from_file = [i[0] for i in
                               re.findall('(^<h[1-4]>(.+?)</h[1-4]>$)',
                                          md_to_html,
                                          re.M)]

    # No invalid headings in the file w/o headings
    if not html_headings_from_file:
        invalid_headings = []
    else:
        # Find invalid headings in the file
        invalid_headings = [key for key in set(html_headings_from_file)
                            if (key not in values) !=
                            bool(re.search(
                                sections_headings['Test'][1], key))]

    # Remove invalid headings from html_headings_from_file
    for index in invalid_headings:
        warnings_list.append(f'unknown html heading "{index}" is used')
        html_headings_from_file = [i for i in html_headings_from_file
                                   if i != index]

    def count_html_headings(heading: str) -> None:
        if html_headings_from_file.count(heading) > 1:
            warnings_list.append(
                f'{html_headings_from_file.count(heading)}'
                f' headings "{heading}" are used')

    # Warn if 2 or more # Setup or # Cleanup are used
    count_html_headings(sections_headings['Setup'][0])
    count_html_headings(sections_headings['Cleanup'][0])

    warn_outside_test_section = 'Heading "{}" from the section "{}" is '\
                                'used \noutside of Test sections.'
    warn_headings_not_in_pairs = 'The number of headings from the section' \
                                 ' "Step" - {}\ndoesn\'t equal to the ' \
                                 'number of headings from the section \n' \
                                 '"Expect" - {} in the test section "{}"'
    warn_required_section_is_absent = '"{}" section doesn\'t exist in ' \
                                      'the Markdown file'
    warn_unexpected_headings = 'Headings "{}" aren\'t expected in the ' \
                               'section "{}"'

    def required_section_exists(
            section: List[str],
            section_name: str,
            prefix: Union[str, Tuple[str, ...]]) -> int:
        res = list(filter(
            lambda t: t.startswith(prefix), section))
        if not res:
            warnings_list.append(
                warn_required_section_is_absent.format(section_name))
            return 0
        else:
            return len(res)

    # Required sections don't exist
    if not required_section_exists(html_headings_from_file,
                                   'Test',
                                   '<h1>Test'):
        return warnings_list

    # Remove Optional heading #Cleanup if it's in the end of document
    if html_headings_from_file[-1] == '<h1>Cleanup</h1>':
        html_headings_from_file.pop()
        # Add # Test heading to close the file
        html_headings_from_file.append(sections_headings['Test'][0])

    index = 0
    while html_headings_from_file:
        # # Step cannot be used outside of test sections.
        if html_headings_from_file[index] == \
                sections_headings['Step'][0] or \
                html_headings_from_file[index] == \
                sections_headings['Step'][1]:
            warnings_list.append(warn_outside_test_section.format(
                html_headings_from_file[index], 'Step'))

        # # Expect cannot be used outside of test sections.
        if html_headings_from_file[index] == \
                sections_headings['Expect'][0] or \
                html_headings_from_file[index] == \
                sections_headings['Expect'][1] or \
                html_headings_from_file[index] == \
                sections_headings['Expect'][2]:
            warnings_list.append(warn_outside_test_section.format(
                html_headings_from_file[index], 'Expect'))

        if html_headings_from_file[index].startswith('<h1>Test'):
            test_section_name = html_headings_from_file[index]
            try:
                html_headings_from_file[index + 1]
            except IndexError:
                break
            for i, v in enumerate(html_headings_from_file[index + 1:]):
                if re.search('^<h1>(Test .*|Test)</h1>$', v):
                    test_section = html_headings_from_file[index + 1:
                                                           index + 1 + i]

                    # Unexpected headings inside Test section
                    unexpected_headings = set(test_section) - \
                        required_headings
                    if unexpected_headings:
                        warnings_list.append(
                            warn_unexpected_headings.
                            format(', '.join(unexpected_headings),
                                   test_section_name))

                    amount_of_steps = required_section_exists(
                        test_section,
                        'Step',
                        tuple(sections_headings['Step']))
                    amount_of_expects = required_section_exists(
                        test_section,
                        'Expect',
                        tuple(sections_headings['Expect']))

                    # # Step isn't in pair with # Expect
                    if amount_of_steps != amount_of_expects != 0:
                        warnings_list.append(warn_headings_not_in_pairs.
                                             format(amount_of_steps,
                                                    amount_of_expects,
                                                    test_section_name))
                    index += i
                    break

        index += 1
        if index >= len(html_headings_from_file) - 1:
            break
    return warnings_list


def return_markdown_file() -> str:
    """ Return path to the markdown file """
    files = '\n'.join(os.listdir())
    reg_exp = r'.+\.md$'
    md_files = re.findall(reg_exp, files, re.M)
    fail_message = "in the current working directory.\n" \
                   "Manual steps couldn't be exported"
    if len(md_files) == 1:
        md_path = os.path.join(os.getcwd(), md_files[0])
    elif len(md_files) == 0:
        md_path = ''
        echo((style(f'Markdown file doesn\'t exist {fail_message}',
                    fg='yellow')))
    else:
        md_path = ''
        echo((style(f'{len(md_files)} Markdown files found {fail_message}',
                    fg='yellow')))
    return md_path


def create_nitrate_case(summary: str) -> Any:
    """ Create new nitrate case """
    import_nitrate()

    # Get category from Makefile
    category = 'Sanity'
    try:
        with open('Makefile', encoding='utf-8') as makefile_file:
            makefile = makefile_file.read()
        category_search = re.search(
            r'echo\s+"Type:\s*(.*)"', makefile, re.M)
        if category_search:
            category = category_search.group(1)
    # Default to 'Sanity' if Makefile or Type not found
    except (IOError, AttributeError):
        pass

    # Create the new test case
    assert nitrate
    category = nitrate.Category(name=category, product=DEFAULT_PRODUCT)
    testcase = nitrate.TestCase(summary=summary, category=category)
    echo(style(f"Test case '{testcase.identifier}' created.", fg='blue'))
    return testcase


def prepare_extra_summary(test: 'tmt.Test') -> str:
    """ extra-summary for export --create test """
    remote_dirname = re.sub('.git$', '', os.path.basename(test.fmf_id['url']))
    if not remote_dirname:
        raise ConvertError("Unable to find git remote url.")
    generated = f"{remote_dirname} {test.name}"
    if test.summary:
        generated += f" - {test.summary}"
    # TODO: remove cast later
    return cast(str, test.node.get('extra-summary', generated))


# avoid multiple searching for general plans (it is expensive)
@ lru_cache(maxsize=None)
def find_general_plan(component: str) -> Any:
    """ Return single General Test Plan or raise an error """
    assert nitrate
    # At first find by linked components
    found = nitrate.TestPlan.search(
        type__name="General",
        is_active=True,
        component__name=f"{component}")
    # Attempt to find by name if no test plan found
    if not found:
        found = nitrate.TestPlan.search(
            type__name="General",
            is_active=True,
            name=f"{component} / General")
    # No general -> raise error
    if not found:
        raise nitrate.NitrateError(
            f"No general test plan found for '{component}'.")
    # Multiple general plans are fishy -> raise error
    if len(found) != 1:
        nitrate.NitrateError(
            "Multiple general test plans found for '{component}' component.")
    # Finally return the one and only General plan
    return found[0]
