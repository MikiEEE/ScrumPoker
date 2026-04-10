import sys, unittest

sys.path.append('..')

from SmallPackage.TaskState import TaskState

class test_taskState(unittest.TestCase):

	def test_update_and_retieval(self):
		state = TaskState()
		data = {'mice':10,'men':'blob','blob':{'10':20}}

		blank_state, status = state.getState(None,'data')
		self.assertEqual(blank_state,dict())

		state.update(data,'data')
		new_state, status = state.getState()
		self.assertEqual(new_state, data)

		one_var, status = state.getState('men','data')
		self.assertEqual(data['men'],one_var)

		data, failed_status = state.getState('mmmmm')
		self.assertEqual(failed_status, -1)


	def test_Free(self):
		state = TaskState()
		data = {'mice':10}

		isFree = state.isFree('mice','data')
		self.assertEqual(isFree, True)

		state.update(data)
		isFree = state.isFree('mice','data')
		self.assertEqual(isFree, False)

		state.free('mice','data')
		isFree = state.isFree('mice','data')
		self.assertEqual(isFree, True)
		



if __name__ == '__main__':
	unittest.main()