


class SmallPID():
    '''
    @class smallPID() - Creates and keeps track of avialable
        Process ID's
    '''

    def __init__(self, max=2**16):
        '''
        @function __init__() - Initializes essential maintenence 
            structures.
        @param max - int - The max number of process ID's.
            Valid process ID's are 0 - (max - 1).
        @return - void
        '''
        self.pastPid = 0
        self.maxPID = max
        self.usedPID = set()
        return


    def newPID(self):
        '''
        @function newPID() - Creates a new valid PID. 
        @return - int - positive integer on success 
            -1 on failure.
        '''
        if len(self.usedPID) < self.maxPID:
            pid = self.pastPid
            while pid%self.maxPID in self.usedPID: pid+=1
            self.usedPID.add(pid%self.maxPID)

            if self.pastPid >= self.maxPID:
                self.pastPid = 0
            self.pastPid = pid
            return pid%self.maxPID
        else:
            return -1


    def freePID(self,pid):
        '''
        @function freePID - removes the pid from the used 
            pid set() making the pid available for future 
            use.
        @return - void
        '''
        if pid in self.usedPID:
            self.usedPID.remove(pid)
        return 