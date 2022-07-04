import os
import re
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, List, Optional, Tuple, Type, cast

import click
import fmf
import pkg_resources

import tmt
from tmt.steps import Method

if TYPE_CHECKING:
    import tmt.steps
    import tmt.steps.discover
    import tmt.steps.provision

# Test data directory name
TEST_DATA = 'data'

# Default test framework
DEFAULT_FRAMEWORK = 'shell'

# The main test output filename
TEST_OUTPUT_FILENAME = 'output.txt'

# Scripts source directory
SCRIPTS_SRC_DIR = pkg_resources.resource_filename(
    'tmt', 'steps/execute/scripts')


class Execute(tmt.steps.Step):
    """
    Run tests using the specified executor.

    Note that the old execution methods 'shell.tmt' and 'beakerlib.tmt'
    have been deprecated and the backward-compatible support for them
    will be dropped in tmt-2.0.

    Use the new L1 metadata attribute 'framework' instead to specify
    which test framework should be used for execution. This allows to
    combine tests using different test frameworks in a single plan.
    """

    # Internal executor is the default implementation
    how = 'tmt'
    data: List[tmt.steps.StepData]

    def __init__(self, plan: tmt.Plan, data: tmt.steps.StepData) -> None:
        """ Initialize execute step data """
        super().__init__(plan=plan, data=data)
        # List of Result() objects representing test results
        self._results: List[tmt.Result] = []

        # List of scripts to install
        self.scripts: List[str] = []

        # Default test framework and mapping old methods
        # FIXME remove when we drop the old execution methods
        self._framework = DEFAULT_FRAMEWORK
        # Map old methods now if there is no run (and thus no wake up)
        if not self.plan.my_run:
            self._map_old_methods()

    def load(self, extra_keys: Optional[List[str]] = None) -> None:
        """ Load test results """
        extra_keys = extra_keys or []
        super().load(extra_keys)
        try:
            results = tmt.utils.yaml_to_dict(self.read('results.yaml'))
            self._results = [
                tmt.Result(data, test) for test, data in results.items()]
        except tmt.utils.FileError:
            self.debug('Test results not found.', level=2)

    def save(self, data: Optional[tmt.steps.StepData] = None) -> None:
        """ Save test results to the workdir """
        data = data or {}
        super().save(data)
        results = dict([
            (result.name, result.export()) for result in self.results()])
        self.write('results.yaml', tmt.utils.dict_to_yaml(results))

    def _map_old_methods(self) -> None:
        """ Map the old execute methods in a backward-compatible way """
        how = self.data[0]['how']
        assert isinstance(how, str)
        matched = re.search(r"^(shell|beakerlib)(\.tmt)?$", how)
        if not matched:
            return
        # Show the old method deprecation warning to users
        self.warn(f"The '{how}' execute method has been deprecated.")
        # Map the old syntax to the appropriate executor
        # shell, beakerlib ---> tmt
        # shell.tmt, beakerlib.tmt ---> tmt
        how = 'tmt'
        self.warn(f"Use 'how: {how}' in the execute step instead (L2).")
        self.data[0]['how'] = how
        # Store shell or beakerlib as the default test framework
        # (used when the framework is not defined in the L1 metadata)
        framework = matched.group(1)
        self.warn(f"Set 'framework: {framework}' in test metadata (L1).")
        self._framework = framework
        self.warn("Support for old methods will be dropped in tmt-2.0.")

    def wake(self) -> None:
        """ Wake up the step (process workdir and command line) """
        super().wake()

        # There should be just a single definition
        if len(self.data) > 1:
            raise tmt.utils.SpecificationError(
                "Multiple execute steps defined in '{}'.".format(self.plan))

        # Choose the right plugin and wake it up
        self._map_old_methods()
        executor = ExecutePlugin.delegate(self, dict(self.data[0]))
        executor.wake()
        self._phases.append(executor)

        # Nothing more to do if already done
        if self.status() == 'done':
            self.debug(
                'Execute wake up complete (already done before).', level=2)
        # Save status and step data (now we know what to do)
        else:
            self.status('todo')
            self.save()

    def show(self) -> None:
        """ Show execute details """
        ExecutePlugin.delegate(self, dict(self.data[0])).show()

    def summary(self) -> None:
        """ Give a concise summary of the execution """
        tests = fmf.utils.listed(self.results(), 'test')
        self.info('summary', f'{tests} executed', 'green', shift=1)

    def go(self) -> None:
        """ Execute tests """
        super().go()

        # Nothing more to do if already done
        if self.status() == 'done':
            self.info('status', 'done', 'green', shift=1)
            self.summary()
            self.actions()
            return

        # Make sure that guests are prepared
        if not self.plan.provision.guests():
            raise tmt.utils.ExecuteError("No guests available for execution.")

        # Execute the tests, store results
        for guest in self.plan.provision.guests():
            for phase in self.phases():
                if phase.enabled_on_guest(guest):
                    phase.go(guest)
                    if isinstance(phase, ExecutePlugin):
                        self._results.extend(phase.results())

        # Give a summary, update status and save
        self.summary()
        self.status('done')
        self.save()

    def requires(self) -> List[str]:
        """
        Packages required for test execution

        Return a list of packages which need to be installed on the
        guest so that tests can be executed. Used by the prepare step.
        """
        requires = set()
        for plugin in self.phases(classes=ExecutePlugin):
            # TODO: remove type: ignore once the self.phases() gets proper type
            requires.update(plugin.requires())  # type: ignore
        return list(requires)

    def results(self) -> List[tmt.Result]:
        """
        Results from executed tests

        Return a dictionary with test results according to the spec:
        https://tmt.readthedocs.io/en/latest/spec/plans.html#execute
        """
        return self._results


class ExecutePlugin(tmt.steps.Plugin):
    """ Common parent of execute plugins """

    # List of all supported methods aggregated from all plugins
    _supported_methods: List[Method] = []

    # Common keys for all execute plugins
    _common_keys = ["exit-first"]

    # Internal executor is the default implementation
    how = 'tmt'

    scripts: Optional[Tuple['Script', 'Script']] = None

    @classmethod
    def base_command(cls,
                     method_class: Optional[Type[click.Command]] = None,
                     usage: Optional[str] = None) -> click.Command:
        """ Create base click command (common for all execute plugins) """

        # Prepare general usage message for the step
        if method_class:
            assert usage is not None
            usage = Execute.usage(method_overview=usage)

        # Create the command
        @click.command(cls=method_class, help=usage)
        @click.pass_context
        @click.option(
            '-h', '--how', metavar='METHOD',
            help='Use specified method for test execution.')
        def execute(context: Any, **kwargs: Any) -> None:
            context.obj.steps.add('execute')
            Execute._save_context(context)

        return execute

    @classmethod
    def options(cls, how: Optional[str] = None) -> List[click.Option]:
        # Add option to exit after the first test failure
        options = [cast(click.Option, click.option(
            '-x', '--exit-first', is_flag=True,
            help='Stop execution after the first test failure.'))]
        return options + super().options(how)

    def go(self, *args: Any, **kwargs: Any) -> None:
        super().go()
        self.verbose(
            'exit-first', self.get('exit-first', default=False),
            'green', level=2)

    @property
    def discover(self) -> tmt.steps.discover.Discover:
        """ Return discover plugin instance """
        # This is necessary so that upgrade plugin can inject a fake discover
        return self.step.plan.discover

    def data_path(
            self,
            test: tmt.Test,
            filename: Optional[str] = None,
            full: bool = False,
            create: bool = False) -> str:
        """
        Prepare full/relative test data directory/file path

        Construct test data directory path for given test, create it
        if requested and return the full or relative path to it (if
        filename not provided) or to the given data file otherwise.
        """
        # Prepare directory path, create if requested
        assert self.step.workdir is not None
        directory = os.path.join(
            self.step.workdir, TEST_DATA, test.name.lstrip('/'))
        if create and not os.path.isdir(directory):
            os.makedirs(os.path.join(directory, TEST_DATA))
        if not filename:
            return directory
        path = os.path.join(directory, filename)
        return path if full else os.path.relpath(path, self.step.workdir)

    def prepare_tests(self) -> List[tmt.Test]:
        """
        Prepare discovered tests for testing

        Check which tests have been discovered, for each test prepare
        the aggregated metadata in a 'metadata.yaml' file under the test
        data directory and finally return a list of discovered tests.
        """
        tests: List[tmt.Test] = self.discover.tests()
        for test in tests:
            metadata_filename = self.data_path(
                test, filename='metadata.yaml', full=True, create=True)
            self.write(
                metadata_filename, tmt.utils.dict_to_yaml(test._metadata))
        return tests

    def prepare_scripts(self, guest: tmt.steps.provision.Guest) -> None:
        """
        Prepare additional scripts for testing
        """
        assert self.scripts

        # Install all scripts on guest
        for script in self.scripts:
            source = os.path.join(
                SCRIPTS_SRC_DIR, os.path.basename(script.path))

            for dest in [script.path] + script.aliases:
                guest.push(
                    source=source,
                    destination=dest,
                    options=["-p", "--chmod=755"])

    def check_shell(self, test: tmt.Test) -> tmt.Result:
        """ Check result of a shell test """
        # Prepare the log path
        data = {'log': self.data_path(test, TEST_OUTPUT_FILENAME),
                'duration': test.real_duration}
        # Process the exit code
        try:
            data['result'] = {0: 'pass', 1: 'fail'}[test.returncode]
        except KeyError:
            data['result'] = 'error'
            # Add note about the exceeded duration
            if test.returncode == tmt.utils.PROCESS_TIMEOUT:
                data['note'] = 'timeout'
                self.timeout_hint(test)
        return tmt.Result(data, test.name, test.result)

    def check_beakerlib(self, test: tmt.Test) -> tmt.Result:
        """ Check result of a beakerlib test """
        # Initialize data, prepare log paths
        data = {'result': 'error',
                'log': [],
                'duration': test.real_duration}
        for log in [TEST_OUTPUT_FILENAME, 'journal.txt']:
            if os.path.isfile(self.data_path(test, log, full=True)):
                data['log'].append(self.data_path(test, log))
        # Check beakerlib log for the result
        try:
            beakerlib_results_file = self.data_path(
                test, 'TestResults', full=True)
            results = self.read(beakerlib_results_file, level=3)
        except tmt.utils.FileError:
            self.debug(f"Unable to read '{beakerlib_results_file}'.", level=3)
            data['note'] = 'beakerlib: TestResults FileError'
            return tmt.Result(data, test.name, test.result)
        try:
            result = re.search(
                'TESTRESULT_RESULT_STRING=(.*)', results).group(1)  # type: ignore
            # States are: started, incomplete and complete
            # FIXME In quotes until beakerlib/beakerlib/pull/92 is merged
            state = re.search(r'TESTRESULT_STATE="?(\w+)"?', results).group(1)  # type: ignore
        except AttributeError:
            self.debug(
                f"No result or state found in '{beakerlib_results_file}'.",
                level=3)
            data['note'] = 'beakerlib: Result/State missing'
            return tmt.Result(data, test.name, test.result)
        # Check if it was killed by timeout (set by tmt executor)
        if test.returncode == tmt.utils.PROCESS_TIMEOUT:
            data['result'] = 'error'
            data['note'] = 'timeout'
            self.timeout_hint(test)
        # Test results should be in complete state
        elif state != 'complete':
            data['result'] = 'error'
            data['note'] = f"beakerlib: State '{state}'"
        # Finally we have a valid result
        else:
            data['result'] = result.lower()
        return tmt.Result(data, test.name, test.result)

    @staticmethod
    def test_duration(start: float, end: float) -> str:
        """ Convert duration to a human readable format """
        return time.strftime("%H:%M:%S", time.gmtime(end - start))

    def timeout_hint(self, test: tmt.Test) -> None:
        """ Append a duration increase hint to the test output """
        output = self.data_path(test, TEST_OUTPUT_FILENAME, full=True)
        self.write(
            output,
            f"\nMaximum test time '{test.duration}' exceeded.\n"
            f"Adjust the test 'duration' attribute if necessary.\n"
            f"https://tmt.readthedocs.io/en/stable/spec/tests.html#duration\n",
            mode='a', level=3)

    def results(self) -> List[tmt.Result]:
        """ Return test results """
        raise NotImplementedError


@dataclass
class Script:
    """ Represents a script provided by the internal executor """
    path: str
    aliases: List[str] = field(default_factory=list)
    related_variables: List[str] = field(default_factory=list)
