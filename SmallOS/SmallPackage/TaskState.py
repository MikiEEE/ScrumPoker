import copy


'''
File Deprecated as of concurrency branch update.
'''


class TaskState():

    '''
    @class taskState - state management class for keeping track of 
        system stats and data of task. 
    '''

    def __init__(self):
        self._state = dict()
        self._state['system'] = dict()
        self._state['data'] = dict()


    def update(self,dict_blob,namespace='data'):
        '''
        @function updateState() - updates the contents of the
            state object. 
        @param dict_blob - objects to be placed in state data. Same Keys will be 
            over written. 
        @param namespace - chooses the namespace ('system' vars or task 'data' vars).
        @return void
        '''
        self._state[namespace].update(dict_blob)
        return


    def isFree(self,key,namespace='data'):
        '''
        @function isFree() - checks to see if the key is free for 
            use in the state dict.
        @param key - int - key to be tested if free for use or not. 
        @param namespace - str - chooses between system variables or data variables within
                the task's state.
        @return - bool - True if the key is available. False if the key is not
            available.
        '''
        testingSpace = self._state[namespace]

        if key in testingSpace:
            return False
        else: 
            return True


    def free(self,key,namespace='data'):
        '''
        @function free() - Will free the selected key from the state.
        @param - key - key to be deleted. 
        @param namespace - str - chooses between system variables or data variables within
                the task's state.
        @return - int - 0 for successful deletion, 1 for no key in namespace. 
        '''
        if key in self._state[namespace]:
            del self._state[namespace][key]
            return 0
        else: 
            return -1 


    def getState(self,key=None,namespace='data'):
        '''
            @function getState - can return the entire state or just one
                element of the state returns a -1 if the requested key does not exist.
            @param key - str -  will return the whole state if equal to None and specific item
                by key if a key is given (str).
            @param namespace - str - chooses between system variables or data variables within
                the task's state.
            @return {}, obj, -1

            NOTE ***Returns a Deep Copy of the state so state must be updated. 
        '''
        if key == None:
            return copy.deepcopy(self._state[namespace]), 0
        if key in self._state[namespace]:
            return copy.deepcopy(self._state[namespace][key]), 0
        else:
            return None, -1



