from functools import cache
import argparse, os, json, pandas, rich
from rich.progress import track, Progress
from concurrent.futures import ProcessPoolExecutor
from scipy.optimize import curve_fit
import matplotlib.pyplot as plt
import rdkit
from rdkit import Chem
from rdkit.Chem import AllChem, Draw, Descriptors, rdFreeSASA
from hydrophobicity_explorer.smiles import monomer_dict, init_dict, checkAndMergeSMILESDicts


class Polymer:
    """
    Holds information related to a rdkit molecule. This includes 
    n: int, number of monomers
    smiles: str, smiles string
    mpn: int, the number of monomers in a comonomer sequence
    flat: mol, 2D structure. Used for pictures
    ratio: str, the ratio of monomers in a polymer with random monomer ordering using a target ratio
    pol_list: list of mols with all optimized conformers
    """

    def __init__(self, n, smiles, mpn=1, ratio=None, pol_list=None):
        self.n = n
        self.mpn = mpn
        self.smiles = smiles
        self.flat = self.get2D()
        self.ratio = ratio
        self.pol_list = pol_list

    def get2D(self):
        return Chem.MolFromSmiles(self.smiles)
    
    def get_pol_list(self, mol):
        pol_list = []
        confs = mol.GetConformers()
        for conf in confs:
            single_conf_mol = Chem.Mol(mol, True)
            single_conf_mol.AddConformer(conf, assignId=True)
            pol_list.append(single_conf_mol)
        self.pol_list = pol_list

def getStaticSettings():
    """
    Reads the settings file if it exists in CWD. Otherwise uses the default settings.
    """
    expected_settings_path = "HXSettings.json"

    if os.path.exists(expected_settings_path):
        from hydrophobicity_explorer.settings import readJson
        print(f"NOTICE: found {expected_settings_path}. This takes presedence over the built-in settings.")
        settings_dict = readJson(expected_settings_path)
    else:
        from hydrophobicity_explorer.settings import default_dict as settings_dict

    return settings_dict


def getJsonArgs(jsonFile, dict):
    """
    Reads run parameters from a file. Allows multiple jobs to be run sequentially with one command.
    """
    with open(jsonFile, 'r') as J: #open json file
        runs_dict = json.load(J) #read it
        for run in runs_dict["runs"]: #so few items the nested for loops shouldn't be a big deal
            run_keys = run.keys()
            for dict_key in dict.keys(): #the keys submitted to the func (derrived from CLI arguments)
                if dict_key not in run_keys: #if there is a key provided by user not in the dict derrived from the json file
                    run[dict_key] = dict[dict_key] #then add it
    run_list = runs_dict["runs"] #now we have a list of runs with all arguments from file and command line.
    return run_list


def getArgs():
    #get commandline arguments
    parser = argparse.ArgumentParser()
    parser.add_argument("-n", type = int, default = 0, help = "The number of monomer or comonomer sequences to repeat.")
    parser.add_argument("-i", "--initiator", type = str, default = "Hydrogen", help = "Initiator Key from initiator dict or SMILES. Defaults to Hydrogen.")
    parser.add_argument("-t", "--terminator", type = str, default = "Hydrogen", help = "Terminator key taken from initiator dict or SMILES. Defaults to Hydrogen.")
    parser.add_argument("-m","--single_monomer", type = str, help = "Monomer key from the included monomer dict. See the -s flag for specifying a monomer that is not included.")
    parser.add_argument("-b", "--comonomer_sequence", type = str, nargs = '*',
                        help = "A series of space-separated monomer SMILES arranged in their repeating sequence. You can add an int preceeding any monomer to represent multiple copies of that monomer. e.g. 2 A B means AAB is the repeating super-monomer. Use quotes surrounding SMILES with problematic characters like = or ()")
    parser.add_argument("-d", "--draw", type = str, help = "Filename for polymer image.")
    parser.add_argument("-v", "--verbose", default = False, action = "store_true", help = "Set increased verbosity. Will draw polymer to polymer.png unless alternate name set by -d option.")
    parser.add_argument("-c","--calculation", type = str, nargs = '*', 
                        help = "Type of calculation(s) to be performed input as a space-separated list. Options are LogP, SA (surface area), MV (Molecular Volume), MHP (Mathers Hydrophobicity Parameter (LogP/SA; each of which will also be reported. Use XMHP to exclude those plots)) and Rg (radius of gyration).")
    parser.add_argument("-s","--save", type = str, help = "The name/path of the file you wish to save the mol to. Supported formats are .pdb, .xyz and .mol")
    parser.add_argument("-r", "--read", type = str, help = "The name/path to file you wish to import. Supported formats are .pdb, .mol and .sdf")
    parser.add_argument("-p", "--plot", default = False, action = "store_true", 
                        help = "Include this option to generate a plot of whatever calculations are specified with -c on polymers from 1 to the n specified with the -n flag. This means the molecule cannot be read from a file with the -r flag. If used with the -f flag multiple files will be saved with names based off the one provided.")
    parser.add_argument("-e", "--export", type = str, help = "Include this option to export a .csv file of all data calculations. Specify the name here.")
    parser.add_argument("-j", "--json", type = str, help = "The path to a compatible .json file with any of the above arguments.")
    parser.add_argument("-q", "--quiet", default = False, action = "store_true", help = "Add this option to suppress the confirmation step which by default prevents calculations from running until the structure of the polymer is approved.")
    parser.add_argument("-a", "--random", default = False, action = "store_true",
            help="Requires the use of the -b flag. Interprets coefficients as desired relative amounts of each comonomer. The ratio provided will be scaled to fit the desired number of monomers. The ordering will be randomized.")
    args, _ = parser.parse_known_args() #second result is for unknown arguments
    #get additional arguments from json file if provided or by default if no args provided.
    vardict = vars(args)
    if args.json is not None:
        run_list = getJsonArgs(args.json, vardict)
    else:
        run_list = [vardict]  #args assumed to be in list later due to above.
    return run_list


def getRepeatUnit(single, co):
    #Two cases: one monomer or comonomers.
    #If both are specified something is wrong.
    if single is not None and co is not None:
        raise TypeError("Cannot specify both single and comonomers")
    #This gives a list of components of a comonomer block or just the string used for single monomer in dict
    repeat_unit = list(filter(None, [single, co]))[0]
    return repeat_unit


def parse_smiles_dict_keys(compound_list, compound_dict):
    #give us the smiles from the dict if a dict key is found. Otherwise assume it is a smiles.
    return [compound_dict[x] if x in compound_dict else x for x in compound_list]


@cache  #avoid multiple lookups if multiple runs with same inputs
def monomer_smi_lookup(m):
    return mono[m]


@cache  #avoid multiple lookups if multiple runs with same inputs
def inator_smi_lookup(i, t):
    given_inators = [i, t]
    #gets from dict if available. Otherwise assume SMILES and continue. There will eventually be an error if this isn't the case.
    smiles_inators = parse_smiles_dict_keys(given_inators, ini)
    return smiles_inators[0], smiles_inators[1]  #init, terminator


def validate_end_group(inator, *, Init=False, Term=False, verbosity=False):
    #checks to see if end groups are symmetrical or if they have a "*" at the correct end
    if not Init and not Term:
        raise ValueError("Need to specify wether end group is terminator or initiator.")
    
    if Init:
        idx = 0  #look at first char of initiator
    else:
        idx = -1  #look at last character of terminator

    if inator != "" and inator[idx] == "*": #the attatchment point does not face the rest of polymer
        if verbosity:
            print("initiator smiles in wrong direction. Converting to mol object.")
        inator = Chem.MolFromSmiles(inator)
    elif inator != inator[::-1] and "*" not in inator:
        raise ValueError("end group smiles is not palendromic yet has no attatchment point specified.")
    else:
        inator = inator.replace("*", "") #remove asterisk if not using rdkit method

    return inator

def get_building_blocks(i,t,m,*, verbosity = False):
    init, term = inator_smi_lookup(i, t) #converts end groups to mol objects if not right direction for text addition.
    
    if type(m) == list:
        #replace any dict keys with corresponding smiles.
        deciphered_dict_keys = parse_smiles_dict_keys(m, mono)

        #we need an accurate count for number of monomers since a grouping specified by -s can be AABBB.
        #In this example, 1 unit of n is really 5 monomers. We want proper notation in figures.
        explicit_coefs = [int(x) if len(str(x)) == 1 else 0 for x in deciphered_dict_keys] #all monomers are (hopefully) 2 atoms or more. Assume others are coefs.
        sum_implicit_coefs = len(deciphered_dict_keys) - sum(explicit_coefs) #number of implicit coefs of 1.
        monomers_per_n = sum(explicit_coefs) + sum_implicit_coefs #total monomer unit count per super-monomer.

        #start with empty repeat unit and concatonate stuff we find in the list.
        repeat_unit = ""
        #ommission of a coeficient implies 1 copy
        repeat_coef = 1
        for element in deciphered_dict_keys:
            try:
                repeat_coef = int(element)  #is this a string of an integer?
            except:
                repeat_unit += repeat_coef * element  #if not, repeat the SMILES as many times as specified (or once if no coef. provided).
                repeat_coef = 1  #reset coef.
    else:
        repeat_unit = monomer_smi_lookup(m) #if not a list, look for the corresponding smiles in the dictionary, will throw error if not included.
        monomers_per_n = 1

    init = validate_end_group(init, Init=True, verbosity=verbosity)
    term = validate_end_group(term, Term=True, verbosity=verbosity)

    return init, term, repeat_unit, monomers_per_n

def attatch_frags(polymer_smiles, *, add_initiator = (False, None), add_terminator = (False, None)): #the initiator and terminator are the kwargs
    pol = Chem.MolFromSmiles(polymer_smiles)
    #get indicies of fake atoms ("*")
    fake_atoms = [a.GetIdx() for a in pol.GetAtoms() if a.GetAtomicNum() == 0]
    #and their neighbors (to which we will actually be attatching.)
    conn_atoms = [pol.GetAtomWithIdx(x).GetNeighbors()[0].GetIdx() for x in fake_atoms]

    #lable the head and tail, accounting for possible absense of one or both inators.
    inators = []
    if add_initiator[0]:
        head = pol.GetAtomWithIdx(conn_atoms[0])
        head.SetProp("atomNote", "head")
        inators.append(add_initiator[1])
        if add_terminator[0]:
            tail = pol.GetAtomWithIdx(conn_atoms[1])  #note index
            tail.SetProp("atomNote", "tail")
            inators.append(add_terminator[1])
    elif add_terminator[0]:
        tail = pol.GetAtomWithIdx(conn_atoms[0])  #note index
        tail.SetProp("atomNote", "tail")
        inators.append(add_terminator[1])
    else:
        raise Exception(f"Unknown combination of inators {add_initiator = }, {add_terminator = }.")

    #set name to what will be used after one loop completes.
    mergedrw = pol
    for inator in inators:
        #see above.
        fake_atoms = [a.GetIdx() for a in inator.GetAtoms() if a.GetAtomicNum() == 0]
        #this time we just isolate atom object instead of index.
        attatch = [inator.GetAtomWithIdx(x).GetNeighbors()[0] for x in fake_atoms][0]
        #lable.
        attatch.SetProp("atomNote", "attatch")
        #put the two mols into the same object (still no bond between them.)
        merged = Chem.CombineMols(inator, mergedrw)
        #change to rwmol object which can be changed.
        mergedrw = Chem.RWMol(merged)

        #indicies of atoms with notes
        attachments = [a.GetIdx() for a in mergedrw.GetAtoms() if a.HasProp('atomNote')]
        #isolating proper index from list to use in bond formation.
        inator_attatchment = [i for i in attachments if mergedrw.GetAtomWithIdx(i).GetProp('atomNote') == "attatch"][0]

        if inator == add_initiator[1]: #inator changes with each pass through the loop
            bond_here = [i for i in attachments if mergedrw.GetAtomWithIdx(i).GetProp('atomNote') == "head"][0]

        if inator == add_terminator[1]:
            bond_here = [i for i in attachments if mergedrw.GetAtomWithIdx(i).GetProp('atomNote') == "tail"][0]
            
        #make bond
        mergedrw.AddBond(bond_here, inator_attatchment,
                         Chem.rdchem.BondType.SINGLE)
        #change label so that atom is not targeted a second time for bond formation.
        mergedrw.GetAtomWithIdx(inator_attatchment).ClearProp('atomNote')

    #count up number of dummy atoms ("*")
    dummies = [a for a in mergedrw.GetAtoms() if a.GetAtomicNum() == 0]
    numDummies = len(dummies)

    #remove the dummy atoms (need to do one at a time because index changes each time).
    for _ in range(numDummies):
        mergedrw.RemoveAtom([a.GetIdx() for a in mergedrw.GetAtoms() if a.GetAtomicNum() == 0][0])

    smi = Chem.MolToSmiles(mergedrw)
    return smi


def add_inator_smiles(smi, init, term, *, verbosity=False):
    #add end groups to the main polymer smiles
    if verbosity:
        print(f"polymer smiles is {smi} before any end groups")

    if type(init) != str:  #i.e. a mol object instead
        smi = "*" + smi
        add_initiator = True  # we will attatch with mol-based methods
    else:
        add_initiator = False
        smi = init + smi  #just attatch as string instead
        if verbosity and init != "":
            print(f"polymer smiles is {smi} after adding initiator smiles")

    if type(term) != str:  #i.e. a mol object instead
        smi = smi + "*"  #same as above but for terminator. Attachment point is at end this time.
        add_terminator = True
    else:
        add_terminator = False
        smi = smi + term  #same as above but for terminator
        if verbosity and init != "":  #no need to print this extra info if adding "" (Hydrogen)
            print(f"polymer smiles is {smi} after adding terminator smiles")

    if add_terminator or add_initiator:
        if verbosity:
            print(f"converting polymer body {smi} to mol object to add frags")
        smi = attatch_frags(smi,
                            add_initiator=(add_initiator, init),
                            add_terminator=(add_terminator, term))

    return smi


def createPolymerObj(i, n, r, t, *, verbosity=False, test=False):
    #given the components of the polymer, like end groups, n and the repeat unit --> Polymer object
    init, term, repeat_unit, m_per_n = get_building_blocks(i,t,r, verbosity=verbosity) #init and term may or may not be mol while i and t are both str.

    if init == "" and term == "":
        addEndgroups = False
        test_smi = repeat_unit
    else:
        addEndgroups = True

    polymer_SMILES = n * repeat_unit

    if test and addEndgroups:  # a parameter used to generate an n=1 image where it is easy to see where end groups attatch
        #if you don't do this and have n=15, the image is very hard to parse visually and some parts of pol will overlap.
        test_smi = add_inator_smiles(repeat_unit, init, term, verbosity=verbosity)
        verbosity = False #turn off verbosity for the next generation because we already display info about endgroup connections the first time.
    
    if addEndgroups:
        polymer_SMILES = add_inator_smiles(polymer_SMILES, init, term, verbosity=verbosity)

    POL = Polymer(n, polymer_SMILES, mpn=m_per_n)

    if test:
        #return test smiles too so it can be previewed. It is fast to make both before confirmation
        #but we do the confirmation before optimizing geometry.
        return test_smi, POL
    else:
        return POL
   
def optPol(pol_h, iters=1500, rdkit_params={"dielectricModel": 2, "dielectricConstant": 78, "NB_THRESH": 100}, progress=None, task_id = None):
    """
    Optimizes the Polymer and uses only the conformers that converged

    pol_h: RDKit Mol object with one, unoptimized conformer
    iters: int, maximum number of iterations to try and converge optimizations

    rdkit_params: dict, must contain the following values:
        dielectricModel: int, model 1 is constant; model 2 is distant-dependent
        dielectricConstant: int, Dielectric constant 78 corresponds to water
        NB_THRESH: int, Value to cut off long-distance interactions. 100 is rdkit default.

    progress and task_id are used for progress bar generation while multiprocessing.
    """

    #unpack rdkit_params
    dielectricModel=rdkit_params["dielectricModel"]
    dielectricConstant=rdkit_params["dielectricConstant"]
    NB_THRESH=rdkit_params["NB_THRESH"]

    props = AllChem.MMFFGetMoleculeProperties(pol_h)
    rdkit.ForceField.rdForceField.MMFFMolProperties.SetMMFFDielectricConstant(props, dielectricConstant)
    rdkit.ForceField.rdForceField.MMFFMolProperties.SetMMFFDielectricModel(props, dielectricModel)

    ff = AllChem.MMFFGetMoleculeForceField(pol_h, props, nonBondedThresh=NB_THRESH)
    # print(f"Initial energy = {ff.CalcEnergy()}")
    ff.Initialize()
    converged = ff.Minimize(iters)
    # print(f"Now calc energy = {ff.CalcEnergy()}")
    # ff = AllChem.MMFFGetMoleculeForceField(pol_h, props, nonBondedThresh=NB_THRESH, confId=CONF_ID)
    # print(f"True calc energy = {ff.CalcEnergy()}")
    if  converged == 1: #i.e. is not converged
        return 0
    else:
        return pol_h


def confirmStructure(smi, *, proceed=None):
    #shows the user an image of the repeat unit with attached end groups
    #save image to temporary file
    print("Please review this preview image:")
    drawPol(Chem.MolFromSmiles(smi), show=True)
    inp = input("Does this look right? [Y/n]")

    #affirmation is y, Y or just hitting enter
    if inp.lower() == "y" or inp == "":
        inp = True
        print("Great! If you wish to bypass this confirmation step, use the -q flag when running this script.")
    else:
        print("Please try adjusting input and try again.")
        quit()
        #aborts so user can retry

    if proceed is not None:
        return inp #used to stop plotting jobs from asking for confirmation for each pol those jobs generate.


def generate_conf_list(POL, nConfs=5, threads=0):
    FLAT = POL.flat
   
    #check mol
    Chem.SanitizeMol(FLAT)
    #opt steps
    pol_h = Chem.AddHs(FLAT)
    #random coords lead to better geometries than using the rules rdkit has. Excluding this kwarg leads to polymers that do not fold properly.
    ids = AllChem.EmbedMultipleConfs(
        pol_h, numConfs=nConfs, useRandomCoords=True,
        numThreads=threads)  #get multiple conformers for better stats
    POL.get_pol_list(pol_h)
    return POL


def make_One_or_More_Polymers(i, n, r, t, *, verbosity=False, plot=False, confirm=False, defaults={"opt_numConfs":5, "opt_numThreads":0, "opt_maxIters":1500, "dielectricModel": 2, "dielectricConstant": 78, "NB_THRESH": 100}):
    # Makes polymers specified by user.
    POL_LIST = []
    if i == "Hydrogen" and t == "Hydrogen":
        addEndgroups = False
        confirm = False
    else:
        addEndgroups = True

    if plot:  #make molecules from n=1 to n specified by user.
        N_array = range(1, n + 1)
    else:
        N_array = [n]

    #this allows us to confirm only once for plotting jobs
    if confirm and addEndgroups:
        approved = False
    else:
        approved = True

    for j in track(N_array, description="[blue] Generating SMILES:", disable = not verbosity):
        if confirm and not approved:
            test_smi, POL = createPolymerObj(i,j,r,t, verbosity=verbosity, test=True)
            approved = confirmStructure(test_smi, proceed=approved)
        if not confirm:
            POL = createPolymerObj(i, j, r, t, verbosity=verbosity)

        POL_LIST.append(POL)

    nConfs=defaults["opt_numConfs"]
    threads=defaults["opt_numThreads"]
    iters=defaults["opt_maxIters"]
    rdkit_params=defaults
    if threads == 0:
        num_proc = os.cpu_count()
    else:
        num_proc = threads

    columns = [rich.progress.SpinnerColumn(style="bold default", finished_text=""),
        "[rich.progress.description]{task.description}",
            rich.progress.BarColumn(),
            "[rich.progress.percentage]{task.percentage:>3.0f}%",
            rich.progress.TimeElapsedColumn()]

    with Progress(*columns) as progress:
        futures = []
        overall_progress_task = progress.add_task("[blue]Embedding Conformers:")

        with ProcessPoolExecutor(max_workers=num_proc) as executor:
            for POL in POL_LIST:
                futures.append(executor.submit(generate_conf_list, POL, nConfs, threads))
            while (n_finished := sum([future.done() for future in futures])) <= len(futures):
                progress.update(overall_progress_task, completed=n_finished, total=len(futures))
                if n_finished == len(futures):
                    break
        POL_LIST = [f.result() for f in futures]

        #prepare inputs
        polh_list = [conf for pol in POL_LIST for conf in pol.pol_list]
        pool_inputs = [(pol_h, iters, rdkit_params) for pol_h in polh_list]        

    with Progress(*columns) as progress:
        futures = []
        overall_progress_task = progress.add_task("[blue]Optimizing Conformers:")

        with ProcessPoolExecutor(max_workers=num_proc) as executor:
            for i, inputs in enumerate(pool_inputs):
                pol_h, iters, rdkit_params = inputs
                futures.append(executor.submit(optPol, pol_h, iters, rdkit_params))
            while (n_finished := sum([future.done() for future in futures])) <= len(futures):
                progress.update(overall_progress_task, completed=n_finished, total=len(futures))
                if n_finished == len(futures):
                    break

    polh_list = [f.result() for f in futures]
    
    #repack the optimized confs with the Polymer objects
    for i, POL in enumerate(POL_LIST):
        offset = i * nConfs
        lower = 0 + offset
        upper = nConfs + offset
        pol_list = polh_list[lower:upper]
        # remove 0s from the list, which are in place of unoptimized Mol
        POL.pol_list = [p for p in pol_list if p != 0]
        
        if len(POL.pol_list) == 0: # all conformations have failed to converge. Tell the user to change something.
            raise Exception("Optimization failed to converge. Increase maxIters valve in mhpSettings.json and rereun.")
        else:
            POL_LIST[i] = POL

    if plot:
        return POL_LIST
    else:
        return POL_LIST[0]

def drawPol(pol, drawName=None, image_size=250, show=False):
    #draws the 2d version of the polymer to an image
    if show:
        img = Chem.Draw.MolToImage(pol, size=(image_size, image_size))
        plt.imshow(img)
        plt.axis('off')  # Remove axis ticks and labels
        plt.show()
        # img.show()
        return

    if type(pol) == list: #save a grid image instead using the polymers in the list
        img = Chem.Draw.MolsToGridImage([POL.flat for POL in pol], legends = [f"n = {(i + 1) * pol[i].mpn}" for i in range(len(pol))], subImgSize=(image_size, image_size))
        #mpn is the number of monomers per "n". This is > 1 when -s is used and multiple monomers or copies of the same monomer are specified.
        img.save(drawName)
    else:
        Chem.Draw.MolToFile(pol, drawName)

    print(f"Saved polymer image to {drawName}")

def read_pol(name, n=None, verbosity=False):
    #reads a polymer file
    ext = name.split(".")[1]
    if n is None:
        raise ValueError("Need to have an n value supplied")
    
    if os.path.exists(name):
        pol_list = []

        #is the file type valid?
        if ext == "pdb":
            pol_h = Chem.MolFromPDBFile(name)
        elif ext == "mol":
            pol_h = Chem.MolFromMolFile(name)
        elif ext == "sdf":
            suppl = Chem.SDMolSupplier(name, removeHs=False)
            for pol in suppl:
                pol_list.append(pol)
            pol_h = pol_list[0]
        else:
            print(f"unsuported extention: {ext} in {name} Please use .pdb, .mol or .sdf") #.xyz cannot be read by rdkit.
            quit()

        #convert to smiles so it can be visualized
        polSMILES = Chem.MolToSmiles(pol_h)
        
        if verbosity:
            print(f"polymer smiles is: {polSMILES}")
        POL = Polymer(n, polSMILES)

        if len(pol_list == 0):
            POL.get_pol_list(pol_h)
        else:
            POL.pol_list = pol_list

        return POL
    else:
        raise FileNotFoundError(name)


def write_pol(name, verbosity=False, pol_list=None):
    #writes a polymer file
    ext = name.split(".")[1]
    if verbosity:
        print(f'writing molecule to {name}')

    pol_list[0] = first_conf
    cid = -1

    #is the file type valid?
    if ext == "sdf":
        writer = Chem.SDWriter(name)
        for pol in pol_list:
            writer.write(pol)

    elif ext == "xyz":
        Chem.MolToXYZFile(first_conf, name, confId=cid)
    elif ext == "pdb":
        Chem.MolToPDBFile(first_conf, name, confId=cid)
    elif ext == "mol":
        Chem.MolToMolFile(first_conf, name, confId=cid)
    else:
        print(f"Unsuported extention: {ext} Please use .pdb, .xyz or .mol")
        quit()

    if verbosity:
        print(f'Success writing molecule to {name}')


def avg_stat(list_of_stats):
    return sum(list_of_stats) / len(list_of_stats)


def Sasa(pol_h):
    # Calculate SASA
    sasa_lst = []

    for mol in pol_h:  #calculate desired property for each molecule in the file.
        radii = Chem.rdFreeSASA.classifyAtoms(mol)
        sasa = Chem.rdFreeSASA.CalcSASA(mol, radii)
        sasa_lst.append(sasa)

    return avg_stat(sasa_lst)


def LogP(pol_h):
    logP_lst = []
    for mol in pol_h:
        logP_lst.append(Chem.Descriptors.MolLogP(mol))

    return avg_stat(logP_lst)


def RadGyration(pol_h):
    rg_list = []

    for mol in pol_h:
        rg_list.append(Chem.rdMolDescriptors.CalcRadiusOfGyration(mol))
    #Chem.Descriptors3D.RadiusOfGyration(pol_h)
    #both seem to give identical results
    return avg_stat(rg_list)


def MolVolume(pol_h, *, grid_spacing=0.2, box_margin=2.0):
    mv_list = []
    for mol in pol_h:
        mv_list.append(
            Chem.AllChem.ComputeMolVolume(mol,
                                          gridSpacing=grid_spacing,
                                          boxMargin=box_margin))

    return avg_stat(mv_list)


def func_exp(x, a, b, c):
    #c = 0
    return a * (x**b) + c

def doCalcs(pol_iter, calcs, defaults={"MV_gridSpacing":0.2, "MV_boxMargin" :2.0}):
    """
    pol_iter: a list of rdkit Mol objects
    cals: set of calculation keys

    Calcs are only done if requested.
    remove entries from set after each calculation and print the unrecognized ones at the end.
    """

    data = {}
    if "SA" in calcs or "MHP" in calcs or "XMHP" in calcs:
        sasa = Sasa(pol_iter)
        if not "XMHP" in calcs:  #if XMHP is included user eXcluisively wants MHP, so we don't return this data.
            data["SA"] = sasa
        calcs.discard("SA")
    if "LOGP" in calcs or "MHP" in calcs or "XMHP" in calcs:
        logP = LogP(pol_iter)
        if not "XMHP" in calcs:  #if XMHP is included user eXcluisively wants MHP, so we don't return this data.
            data["LogP"] = logP
        calcs.discard("LOGP")
    if "RG" in calcs:
        rg = RadGyration(pol_iter)
        data["Rg"] = rg
        calcs.discard("RG")
    if "MV" in calcs:
        mv = MolVolume(pol_iter,
                       box_margin=defaults["MV_boxMargin"],
                       grid_spacing=defaults["MV_gridSpacing"])
        data["MV"] = mv
        calcs.discard("MV")
    if "MHP" in calcs or "XMHP" in calcs:
        mhp = logP / sasa
        data["LogP/SA"] = mhp
        calcs.discard("MHP")
        calcs.discard("XMHP")
    if len(calcs) > 0:
        print(f"Unrecognized calculation(s): {calcs}. Use SA, LogP, MV, MHP, XMHP or Rg")
    return data

def makePlot(pol_list, calculations, verbosity=False, data_marker='o', fig_filename="Size-dependent-stats.png"):
    #creates a plot from the calcs of several polymers
    units = {
        "LogP/SA": "Angstroms^-2",
        "LogP": "",
        "Rg": "Angstroms",
        "SA": "Angstroms^2",
        "MV": "Molar Volume"
    }
    dicts = []
    for POL in pol_list:
        calcs = set([calc.upper() for calc in calculations])
        pol_data = doCalcs(POL.pol_list, calcs)
        pol_data["N"] = POL.n * POL.mpn
        pol_data["smi"] = POL.smiles
        if POL.ratio is not None:
            pol_data["Monomer Ratio"] = POL.ratio
            exclude = 3
        else:
            exclude = 2
        dicts.append(pol_data)
    data = {k: [d[k] for d in dicts] for k in dicts[0]} #merge dictionaries of data from all polymers into one sensible format.
    
    ncols = len(data) - exclude #we don't plot N, smiles nor ratios (if present).

    if ncols == 1: #matplotlib got angry at me for trying to make a plot with only one subplot. Use plt.plot to avoid this.
        calc_key = [k if k != "XMHP" or k != "MHP" else "LogP/SA" for k in data.keys()][0] #use given calc as key unless XMHP, then use MHP.
        if calc_key == "Rg":
            #add regression.
            try:
                popt, _ = curve_fit(func_exp, data["N"], data[calc_key])
                print(f"exponential regression parameters: {popt}")
                regresion_curve = plt.plot(
                    data["N"],
                    func_exp(data["N"], *popt),
                    color='xkcd:teal',
                    label="fit: {:.3f}*x^{:.3f}+{:.3f}".format(*popt))
                points = plt.plot(data["N"],
                                  data[calc_key],
                                  data_marker,
                                  label="Rg Data")
                plt.legend()
            except:
                print("Could not complete regression.")

        plt.plot(data["N"], data[calc_key], data_marker)
        plt.title(f'{calc_key} vs n')
        plt.xlabel('n')
        plt.ylabel(f'{calc_key} ({units[calc_key]})')
    else:
        #need to make multiple subplots if multiple calcs requested.
        _, axis = plt.subplots(ncols=ncols)
        series = 0
        for key in data:
            #we can't plot N vs N or anything to do with smiles
            if key != "N" and key != "smi" and key != "Monomer Ratio":
                if key == "Rg":
                    try:  #attempt regression
                        popt, _ = curve_fit(func_exp, data["N"], data[key])
                    except:  #plot data anyway if regression fails
                        print("Could not complete regression.")
                        axis[series].scatter(data["N"], data[key])
                    else:  #code executed when no error in try block.
                        #report regression parameters
                        print(f"exponential regression parameters: {popt}")
                        #plot regression curve
                        _, = axis[series].plot(
                            data["N"],
                            func_exp(data["N"], *popt),
                            color='xkcd:teal',
                            label="fit: {:.3f}*x^{:.3f}+{:.3f}".format(*popt))
                        # plot points
                        _ = axis[series].scatter(data["N"],
                                                 data[key],
                                                 label="Rg Data")
                        #create legend for this subplot
                        axis[series].legend()
                else:
                    axis[series].scatter(data["N"], data[key])
                axis[series].set_title(f"{key} vs n")
                series += 1
    figname = fig_filename
    plt.savefig(figname, bbox_inches='tight')
    print(f'Saved plot to {figname}')
    df = pandas.DataFrame(data)
    df.sort_values(by=["N"], inplace=True)
    if verbosity:
        plt.show()
    return df


def exportToCSV(exptName, dataframe):
    pandas.DataFrame.to_csv(dataframe, exptName, index=False)
    print(f"Done exporting data to {exptName}.")


def main(**kwargs):
    default_dict = getStaticSettings()
    run_list = getArgs()

    #merge user-created smiles with built-in dicts and make accessible to all funcs
    global ini, mono
    ini, mono = checkAndMergeSMILESDicts(init_dict, monomer_dict)
    
    for vardict in run_list:
        for key in kwargs: 
            vardict[key] = kwargs[key] #assign all keyword arguments to proper place in var dictionary

        if vardict["read"] is None: #then get polymer parameters from CLI arguments.
            repeat_unit = getRepeatUnit(vardict["single_monomer"], vardict["comonomer_sequence"])
            if not vardict["random"]:
                if vardict["plot"]:
                    POL_LIST = make_One_or_More_Polymers(
                        vardict["initiator"],
                        vardict["n"],
                        repeat_unit,
                        vardict["terminator"],
                        verbosity=vardict["verbose"],
                        plot=vardict["plot"],
                        confirm=not vardict["quiet"],
                        defaults=default_dict)
                else:
                    POL = make_One_or_More_Polymers(
                        vardict["initiator"],
                        vardict["n"],
                        repeat_unit,
                        vardict["terminator"],
                        verbosity=vardict["verbose"],
                        plot=vardict["plot"],
                        confirm=not vardict["quiet"],
                        defaults=default_dict)
            else:
                if type(repeat_unit) != list:
                    raise TypeError("comonomers must be specified with -b if -a is used.")
                init, term = inator_smi_lookup(vardict["initiator"], vardict["terminator"])
                init = validate_end_group(init, Init=True)
                term = validate_end_group(term, Term=True)
                deciphered_dict_keys = parse_smiles_dict_keys(repeat_unit, mono)
                if vardict["plot"]:
                    n_iter = reversed(range(1, vardict["n"] + 1))
                    POL_LIST = []
                else:
                    n_iter = [vardict["n"]]

                # this is in reverse order since larger polymers are more likely to fail.
                # Let this happen at the start of the run so user knows to change settings.
                for n in n_iter:
                    import hydrophobicity_explorer.random_polymer_to_mol_file as randPol
                    polymer_body_smiles, ratio = randPol.makePolymerBody_ratio(deciphered_dict_keys, n, verbo=vardict["verbose"])
                    if polymer_body_smiles is None:
                        break
                    polSMILES = add_inator_smiles(polymer_body_smiles, init,
                                                  term)
                    POL = Polymer(n, polSMILES, ratio=ratio)
                    POL = optPol(POL,
                                nConfs=default_dict["opt_numConfs"],
                                threads=default_dict["opt_numThreads"],
                                iters=default_dict["opt_maxIters"],
                                rdkit_params=default_dict)
                    if vardict["plot"]:
                        POL_LIST.append(POL)

        else: #get mol from file
            if vardict["single_monomer"] is not None or vardict["comonomer_sequence"] is not None:
                raise Exception("No monomers may be specified when reading from a file.")
            elif vardict["plot"]:
                raise Exception("You may not plot data read from a file.") #we should be able to check for other files with name convention "{name}_{n}.{ext}"
            elif vardict["n"] == None:
                raise Exception("You need to specify the number of monomers in polymers read from a file.")
            POL = read_pol(vardict["read"], n=vardict["n"], verbosity=vardict["verbose"])

        #saving the polymer to a file.
        if vardict["save"] is not None: #technically nothing wrong with using this as a roundabout way of converting between filetypes                
            if vardict["plot"]:
                base = vardict["save"].split(".")[0]
                ext = vardict["save"].split(".")[1]
                for i, mol in enumerate(POL_LIST):
                    name = f"{base}_{i + 1}.{ext}"
                    write_pol(name, pol_list=POL.pol_list)
            else:
                write_pol(vardict["save"],
                            pol_list=POL.pol_list,
                            verbosity=vardict["verbose"])

        #drawing a picture of the polymer.
        drawName = None
        if vardict["plot"]:
            pol = POL_LIST  #submit this list of mols for use in grid image. If a list is detected, the flats will be pulled out.
        else:
            pol = POL.flat

        if vardict["draw"] is not None:
            drawName = f'{vardict["draw"].split(".")[0]}.png'
        elif vardict["verbose"]:
            drawName = default_dict["drawing_default"]

        if drawName is not None:
            drawPol(pol, drawName, image_size=default_dict["drawing_subImgSize_edge"])

        #CALCULATIONS
        if vardict["verbose"]:
            print(f'requested calculations are {vardict["calculation"]}')
        if vardict["calculation"] is not None:
            if not vardict["plot"]:
                calcs = set([calc.upper() for calc in vardict["calculation"]])
                data = doCalcs(
                    POL.pol_list, calcs,
                    defaults=default_dict)  #use set to remove duplicates
                data["N"] = vardict["n"] * POL.mpn
                data["smi"] = POL.smiles
                data = {k: [data[k]] for k in data} #values in dict need to be lists
                if vardict["random"]:
                    data["Monomer Ratio"] = POL.ratio  #already in list form
                dataframe = pandas.DataFrame(data)
            else:
                dataframe = makePlot(
                    POL_LIST,
                    vardict["calculation"],
                    verbosity=vardict["verbose"],
                    data_marker=default_dict["plot_dataPoint"],
                    fig_filename=default_dict["plot_Filename"])

            #we should always show data if it is collected.
            print(dataframe)

            if vardict["export"] is not None:
                exportToCSV(vardict["export"], dataframe)
        elif vardict["calculation"] is None and vardict["export"] is not None:
            raise Exception("You cannot export data if none were collected.")

        if len(run_list) > 1:
            print("\n")  #separating runs visually if more than one.


if __name__ == "__main__":
    main()
