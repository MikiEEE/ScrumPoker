



def is_iterator(obj):
    if (
            hasattr(obj, '__iter__') and
            hasattr(obj, '__next__') and 
            callable(obj.__iter__) and
            obj.__iter__() is obj
        ):
        return True
    else:
        return False
