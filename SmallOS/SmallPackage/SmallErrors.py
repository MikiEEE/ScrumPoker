
class MaxProcessError(Exception):
	'''
	Raised When Process Count Exceeds Limit Set In
	length attribute in the OSlist class. 
	'''
	pass

class PIDError(Exception):
	'''
	Raised When PID On Task Is Attempted To Be Set More 
	Than Once.
	'''
	pass

class StateDictionaryKeyError(Exception):
	'''
	Raised When There Is A Clash In Dictionary Keys For State
	Updating Actions. 
	'''
	pass

class AsyncSuspensionError(Exception):
	'''
	Raised When Someone Tries To Suspend an Async Function.
	'''
	pass


class UnsupportedAwaitableError(Exception):
	'''
	Raised when a task yields an awaitable the smallOS scheduler does not own.
	'''
	pass


class TaskCancelledError(Exception):
	'''
	Raised inside a waiting task when a joined task is cancelled.
	'''
	pass
