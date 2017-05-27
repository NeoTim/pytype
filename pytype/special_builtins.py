"""Custom implementations of builtin types."""

from pytype import abstract
from pytype import function


class TypeNew(abstract.PyTDFunction):
  """Implements type.__new__."""

  def call(self, node, func, args):
    if len(args.posargs) == 4:
      self._match_args(node, args)  # May raise FailedFunctionCall.
      cls, name_var, bases_var, class_dict_var = args.posargs
      try:
        bases = list(abstract.get_atomic_python_constant(bases_var))
        if not bases:
          bases = [self.vm.convert.object_type]
        variable = self.vm.make_class(
            node, name_var, bases, class_dict_var, cls)
      except abstract.ConversionError:
        pass
      else:
        return node, variable
    return super(TypeNew, self).call(node, func, args)


class IsInstance(abstract.AtomicAbstractValue):
  """The isinstance() function."""

  # Minimal signature, only used for constructing exceptions.
  _SIGNATURE = function.Signature(
      "isinstance", ("obj", "type_or_types"), None, set(), None, {}, {}, {})

  def __init__(self, vm):
    super(IsInstance, self).__init__("isinstance", vm)
    # Map of True/False/None (where None signals an ambiguous bool) to
    # vm values.
    self._vm_values = {
        True: vm.convert.true,
        False: vm.convert.false,
        None: vm.convert.primitive_class_instances[bool],
    }

  def call(self, node, _, args):
    try:
      if len(args.posargs) != 2:
        raise abstract.WrongArgCount(self._SIGNATURE, args, self.vm)
      elif args.namedargs.keys():
        raise abstract.WrongKeywordArgs(
            self._SIGNATURE, args, self.vm, args.namedargs.keys())
      else:
        result = self.vm.program.NewVariable()
        for left in args.posargs[0].bindings:
          for right in args.posargs[1].bindings:
            pyval = self._is_instance(left.data, right.data)
            result.AddBinding(self._vm_values[pyval],
                              source_set=(left, right), where=node)
    except abstract.InvalidParameters as ex:
      self.vm.errorlog.invalid_function_call(self.vm.frames, ex)
      result = self.vm.convert.create_new_unsolvable(node)
    return node, result

  def _is_instance(self, obj, class_spec):
    """Check if the object matches a class specification.

    Args:
      obj: An AtomicAbstractValue, generally the left hand side of an
          isinstance() call.
      class_spec: An AtomicAbstractValue, generally the right hand side of an
          isinstance() call.

    Returns:
      True if the object is derived from a class in the class_spec, False if
      it is not, and None if it is ambiguous whether obj matches class_spec.
    """
    if isinstance(obj, abstract.AMBIGUOUS_OR_EMPTY):
      return None
    # Assume a single binding for the object's class variable.  If this isn't
    # the case, treat the call as ambiguous.
    cls_var = obj.get_class()
    if cls_var is None:
      return None
    try:
      obj_class = abstract.get_atomic_value(cls_var)
    except abstract.ConversionError:
      return None

    # Determine the flattened list of classes to check.
    classes = []
    ambiguous = self._flatten(class_spec, classes)

    for c in classes:
      if c in obj_class.mro:
        return True  # A definite match.
    # No matches, return result depends on whether _flatten() was
    # ambiguous.
    return None if ambiguous else False

  def _flatten(self, value, classes):
    """Flatten the contents of value into classes.

    If value is a Class, it is appended to classes.
    If value is a PythonConstant of type tuple, then each element of the tuple
    that has a single binding is also flattened.
    Any other type of value, or tuple elements that have multiple bindings are
    ignored.

    Args:
      value: An abstract value.
      classes: A list to be modified.

    Returns:
      True iff a value was ignored during flattening.
    """
    if isinstance(value, abstract.Class):
      # A single class, no ambiguity.
      classes.append(value)
      return False
    elif isinstance(value, abstract.Tuple):
      # A tuple, need to process each element.
      ambiguous = False
      for var in value.pyval:
        if (len(var.bindings) != 1 or
            self._flatten(var.bindings[0].data, classes)):
          # There were either multiple bindings or ambiguity deeper in the
          # recursion.
          ambiguous = True
      return ambiguous
    else:
      return True


class SuperInstance(abstract.AtomicAbstractValue):
  """The result of a super() call, i.e., a lookup proxy."""

  def __init__(self, cls, obj, vm):
    super(SuperInstance, self).__init__("super", vm)
    self.cls = self.vm.convert.super_type
    self.super_cls = cls
    self.super_obj = obj
    self.get = abstract.NativeFunction("__get__", self.get, self.vm)
    self.set = abstract.NativeFunction("__set__", self.set, self.vm)

  def get(self, node, *unused_args, **unused_kwargs):
    return node, self.to_variable(node)

  def set(self, node, *unused_args, **unused_kwargs):
    return node, self.to_variable(node)

  def get_special_attribute(self, node, name, valself):
    if name == "__get__":
      return self.get.to_variable(node)
    elif name == "__set__":
      return self.set.to_variable(node)
    else:
      return super(SuperInstance, self).get_special_attribute(
          node, name, valself)

  def get_class(self):
    return self.cls

  def call(self, node, _, args):
    self.vm.errorlog.not_callable(self.vm.frames, self)
    return node, abstract.Unsolvable(self.vm).to_variable(node)


class Super(abstract.PyTDClass):
  """The super() function. Calling it will create a SuperInstance."""

  # Minimal signature, only used for constructing exceptions.
  _SIGNATURE = function.Signature(
      "super", ("cls", "self"), None, set(), None, {}, {}, {})

  def __init__(self, vm):
    super(Super, self).__init__(
        "super", vm.lookup_builtin("__builtin__.super"), vm)
    self.module = "__builtin__"

  def call(self, node, _, args):
    result = self.vm.program.NewVariable()
    if len(args.posargs) == 1:
      # TODO(kramm): Add a test for this
      for cls in args.posargs[0].bindings:
        result.AddBinding(SuperInstance(cls.data, None, self.vm), [cls], node)
    elif len(args.posargs) == 2:
      for cls in args.posargs[0].bindings:
        if not isinstance(cls.data, (abstract.Class,
                                     abstract.AMBIGUOUS_OR_EMPTY)):
          bad = abstract.BadParam(
              name="cls", expected=self.vm.convert.type_type.data[0])
          raise abstract.WrongArgTypes(
              self._SIGNATURE, args, self.vm, bad_param=bad)
        for obj in args.posargs[1].bindings:
          result.AddBinding(
              SuperInstance(cls.data, obj.data, self.vm), [cls, obj], node)
    else:
      raise abstract.WrongArgCount(self._SIGNATURE, args, self.vm)
    return node, result