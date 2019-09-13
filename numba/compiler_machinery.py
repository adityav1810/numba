from __future__ import print_function, absolute_import

import timeit
from abc import abstractmethod, ABCMeta
from collections import namedtuple, OrderedDict
import inspect
import traceback
from numba.compiler_lock import global_compiler_lock
from numba import errors
from . import config, utils, transforms, six
from .tracing import event
from .postproc import PostProcessor

# terminal color markup
_termcolor = errors.termcolor()


class SimpleTimer(object):
    """
    A simple context managed timer
    """

    def __enter__(self):
        self.ts = timeit.default_timer()
        return self

    def __exit__(self, *exc):
        self.elapsed = timeit.default_timer() - self.ts


@six.add_metaclass(ABCMeta)
class CompilerPass(object):

    @abstractmethod
    def __init__(self, *args, **kwargs):
        self._analysis = None
        self._pass_id = None

    @classmethod
    def name(cls):
        return cls._name

    @property
    def pass_id(self):
        return self._pass_id

    @pass_id.setter
    def pass_id(self, val):
        self._pass_id = val

    @property
    def analysis(self):
        return self._analysis

    @analysis.setter
    def analysis(self, val):
        self._analysis = val

    def run_initialisation(self, *args, **kwargs):
        return False

    @abstractmethod
    def run_pass(self, *args, **kwargs):
        pass

    def run_finaliser(self, *args, **kwargs):
        """
        User defined finaliser
        """
        return False

    def get_analysis_usage(self, AU):
        """Override to set analysis usage
        """
        pass

    def get_analysis(self, pass_name):
        return self._analysis[pass_name]


class FunctionPass(CompilerPass):
    pass


class AnalysisPass(CompilerPass):
    pass


class LoweringPass(CompilerPass):
    pass


class AnalysisUsage(object):
    """This looks and behaves like LLVM's AnalysisUsage because its like that.
    """

    def __init__(self):
        self._required = set()
        self._preserved = set()

    def get_required_set(self):
        return self._required

    def get_preserved_set(self):
        return self._preserved

    def add_required(self, pss):
        self._required.add(pss)

    def add_preserved(self, pss):
        self._preserved.add(pss)

    def __str__(self):
        return "required: %s\n" % self._required


_DEBUG = False


def debug_print(*args, **kwargs):
    if _DEBUG:
        print(*args, **kwargs)


pass_timings = namedtuple('pass_timings', 'init run finalise')


class PassManager(object):
    """
    The PassManager is a named instance of a particular compilation pipeline
    """
    # TODO: Eventually enable this, it enforces self consistency after each pass
    _ENFORCING = False

    def __init__(self, pipeline_name):
        """
        Create a new pipeline with name "pipeline_name"
        """
        self.passes = []
        self.exec_times = OrderedDict()
        self._finalized = False
        self._analysis = None
        self._print_after = None
        self.pipeline_name = pipeline_name

    def _validate_pass(self, pass_cls):
        if not (isinstance(pass_cls, str) or (inspect.isclass(pass_cls) and issubclass(pass_cls, CompilerPass))):
            msg = "Pass must be referenced by name or be a subclass of a CompilerPass. Have %s" % pass_cls
            raise TypeError(msg)
        if isinstance(pass_cls, str):
            pass_cls = _pass_registry.find_by_name(pass_cls)
        else:
            if not _pass_registry.is_registered(pass_cls):
                raise ValueError("Pass %s is not registered" % pass_cls)

    def add_pass(self, pss, description=""):
        """
        Add a pass to the PassManager
        """
        self._validate_pass(pss)
        func_desc_tuple = (pss, description)
        self.passes.append(func_desc_tuple)
        self._finalized = False

    def add_pass_after(self, pass_cls, location):
        """
        Add a pass to the PassManager after the pass "location"
        """
        assert self.passes
        self._validate_pass(pass_cls)
        self._validate_pass(location)
        for idx, (x, _) in enumerate(self.passes):
            if x == location:
                break
        else:
            raise ValueError("Could not find pass %s" % location)
        self.passes.insert(idx + 1, (pass_cls, str(pass_cls)))
        # if a pass has been added, it's not finalized
        self._finalized = False

    def _debug_init(self):
        # determine after which passes IR dumps should take place
        print_passes = []
        if config.DEBUG_PRINT_AFTER != "none":
            if config.DEBUG_PRINT_AFTER == "all":
                print_passes = [x.name() for (x, _) in self.passes]
            else:
                # we don't validate whether the named passes exist in this pipeline
                # the compiler may be used reentrantly and different pipelines may
                # contain different passes
                splitted = config.DEBUG_PRINT_AFTER.split(',')
                print_passes = [x.strip() for x in splitted]
        return print_passes

    def finalize(self):
        """
        Finalize the PassManager, after which no more passes may be added and
        analysis etc is completed.
        """
        self._analysis = self.dependency_analysis()
        self._print_after = self._debug_init()
        self._finalized = True

    @property
    def finalized(self):
        return self._finalized

    def _patch_error(self, desc, exc):
        """
        Patches the error to show the stage that it arose in.
        """
        newmsg = "{desc}\n{exc}".format(desc=desc, exc=exc)

        # For python2, attach the traceback of the previous exception.
        if not utils.IS_PY3 and config.FULL_TRACEBACKS:
            # strip the new message to just print the error string and not
            # the marked up source etc (this is handled already).
            stripped = _termcolor.errmsg(newmsg.split('\n')[1])
            fmt = "Caused By:\n{tb}\n{newmsg}"
            newmsg = fmt.format(tb=traceback.format_exc(), newmsg=stripped)

        exc.args = (newmsg,)
        return exc

    @global_compiler_lock  # this need a lock, likely calls LLVM
    def _runPass(self, index, pss, internal_state):
        mutated = False

        def check(func, compiler_state):
            mangled = func(compiler_state)
            if mangled not in (True, False):
                msg = ("CompilerPass implementations should return True/False. "
                       "CompilerPass with name '%s' did not.")
                raise ValueError(msg % pss.name())
            return mangled

        # wire in the analysis info so it's accessible
        pss.analysis = self._analysis

        with SimpleTimer() as init_time:
            mutated |= check(pss.run_initialisation, internal_state)
        with SimpleTimer() as pass_time:
            mutated |= check(pss.run_pass, internal_state)
        with SimpleTimer() as finalise_time:
            mutated |= check(pss.run_finaliser, internal_state)

        if self._ENFORCING:
            # TODO: Add in self consistency enforcement for
            # `func_ir._definitions` etc
            if _pass_registry.get(pss.__class__).mutates_CFG:
                if mutated: # block level changes, rebuild all
                    PostProcessor(internal_state.func_ir).run()
                else: # CFG level changes rebuild CFG
                    internal_state.func_ir.blocks = transforms.canonicalize_cfg(
                        internal_state.func_ir.blocks)

        # inject runtimes
        pt = pass_timings(init_time.elapsed, pass_time.elapsed,
                          finalise_time.elapsed)
        self.exec_times["%s_%s" % (index, pss.name())] = pt

        # debug print after this pass?
        if pss.name() in self._print_after:
            print(("%s: %s" % (self.pipeline_name, pss.name())).center(80, '-'))
            internal_state.func_ir.dump()

    def run(self, state):
        from numba.compiler import _EarlyPipelineCompletion
        if not self.finalized:
            raise RuntimeError("Cannot run non-finalised pipeline")

        # walk the passes and run them
        for idx, (pss, pass_desc) in enumerate(self.passes):
            try:
                event("-- %s" % pass_desc)
                pass_inst = _pass_registry.get(pss).pass_inst
                if isinstance(pass_inst, CompilerPass):
                    self._runPass(idx, pass_inst, state)
                else:
                    raise BaseException("Legacy pass in use")
            except _EarlyPipelineCompletion as e:
                raise e
            except Exception as e:
                msg = "Failed in %s mode pipeline (step: %s)" % \
                    (self.pipeline_name, pass_desc)
                patched_exception = self._patch_error(msg, e)
                raise patched_exception

    def dependency_analysis(self):
        """
        Computes dependency analysis
        """
        deps = dict()
        for (pss, _) in self.passes:
            x = _pass_registry.get(pss).pass_inst
            au = AnalysisUsage()
            x.get_analysis_usage(au)
            deps[type(x)] = au

        requires_map = dict()
        for k, v in deps.items():
            requires_map[k] = v.get_required_set()

        def resolve_requires(key, rmap):
            def walk(lkey, rmap):
                dep_set = set(rmap[lkey])
                if dep_set:
                    for x in dep_set:
                        dep_set |= (walk(x, rmap))
                    return dep_set
                else:
                    return set()
            ret = set()
            for k in key:
                ret |= walk(k, rmap)
            return ret

        dep_chain = dict()
        for k, v in requires_map.items():
            dep_chain[k] = set(v) | (resolve_requires(v, requires_map))

        return dep_chain


pass_info = namedtuple('pass_info', 'pass_inst mutates_CFG analysis_only')


class PassRegistry(object):
    """
    Pass registry singleton class.
    """

    _id = 0

    _registry = dict()

    def register(self, mutates_CFG, analysis_only):
        def make_festive(pass_class):
            assert not self.is_registered(pass_class)
            assert not self._does_pass_name_alias(pass_class.name())
            pass_class.pass_id = self._id
            self._id += 1
            self._registry[pass_class] = pass_info(pass_class(), mutates_CFG,
                                                   analysis_only)
            return pass_class
        return make_festive

    def is_registered(self, clazz):
        return clazz in self._registry.keys()

    def get(self, clazz):
        assert self.is_registered(clazz)
        return self._registry[clazz]

    def _does_pass_name_alias(self, check):
        for k, v in self._registry.items():
            if v.pass_inst.name == check:
                return True
        return False

    def find_by_name(self, class_name):
        assert isinstance(class_name, str)
        for k, v in self._registry.items():
            if v.pass_inst.name == class_name:
                return v
        else:
            raise ValueError("No pass with name %s is registered" % class_name)

    def dump(self):
        for k, v in self._registry.items():
            print("%s: %s" % (k, v))


_pass_registry = PassRegistry()
del PassRegistry

register_pass = _pass_registry.register