import array
from decimal import Decimal
import json
from PIL import Image
import os
import sqlite3
from io import BytesIO
import struct
import sys
import tornado.ioloop
import tornado.web
from tornado.options import define, options

import zlib
define("world", default="map.sqlite", help="World to make map from")
define("generate", default=False, help="Just generate the tiles and exit")

def unsignedToSigned(i, max_positive):
    if i < max_positive:
        return i
    else:
        return i - 2*max_positive

def coord2pos(x,y,z):
    return(x+y*4096+z*16777216)

def getIntegerAsBlock(i):
    x = unsignedToSigned(i % 4096, 2048)
    i = int((i - x) / 4096)
    y = unsignedToSigned(i % 4096, 2048)
    i = int((i - y) / 4096)
    z = unsignedToSigned(i % 4096, 2048)
    return x,y,z

class Node:
    def __init__(self, x, y, z, parent, content):
        self.x=x
        self.y=y
        self.z=z
        self.parent=parent
        self.content = content
        self.absx = parent.x*16+x
        self.absy = parent.y*16+y
        self.absz = parent.z*16+z
    def __repr__(self):
        return("Node({},{},{}) {}".format(self.absx, self.absy, self.absz, self.content))

    def getTexture(self):
        if "dirt" in self.content:
            self.content = "default:dirt"
        if "cactus" in self.content:
            self.content = "default:cactus_top"
        if "water" in self.content:  
            self.content = "default:water"
        if "stone" in self.content:  
            self.content = "default:stone"
        (mod, item) = self.content.split(":")
        path = "/usr/share/minetest/games/minetest_game/mods/{}/textures/{}_{}".format(mod,mod,item)+'.png'
        if not os.path.exists(path):
            print("No texture found for ",self.content)
            path = "/usr/share/minetest/games/minetest_game/mods/default/textures/default_mese_block.png"
        return(path)

class Block:
    x = None
    y = None
    z = None
    ps = None

    def __init__(self, ps, blob):
        self.ps = ps
        self.blob = blob
        self.x,self.y,self.z = getIntegerAsBlock(ps)
        self.version = blob[0]
        self.flags = blob[1]
        self.is_underground = self.flags & 1 != 0
        self.day_night_differs = self.flags & 2 != 0
        self.lighting_expired = self.flags & 4 != 0
        self.generated = self.flags & 8 != 0
        self.content_width = blob[2]
        self.params_width = blob[3]
        if self.version != 25:
            print(version)

    def __repr__(self):
        return("Block({},{},{}) {}".format(self.x,self.y,self.z, self.is_underground and "underground" or ""))

    def parse_blob(self):
        """Unzip and parse the blob. Everything we get is big endian. Each block contains 16*16*16 nodes, a node is the ingame block size. """
        dec_o = zlib.decompressobj()
        (self.param0, self.param1, self.param2) = struct.unpack("8192s4096s4096s", dec_o.decompress(self.blob[4:]))
        self.param0 = array.array("H", self.param0)
        self.param0.byteswap()
        #import pdb;pdb.set_trace()
        tail = dec_o.unused_data
        dec_o = zlib.decompressobj() #Must make new obj or .unused_data will get messed up.
        blah = dec_o.decompress(tail) #throw away metadata
         
        (static_version, static_count,) = struct.unpack(">BH", dec_o.unused_data[0:3])
        ptr=3
        if static_count:
            for i in range(static_count):
                (object_type, pos_x_nodes, pos_y_nodes, pos_z_nodes, data_size) = struct.unpack(">BiiiH", dec_o.unused_data[ptr:ptr+15])
                ptr = ptr+15+data_size
        
        (self.timestamp,) = struct.unpack(">I", dec_o.unused_data[ptr:ptr+4])
        if self.timestamp == 0xffffffff: #This is define as as unknown timestamp
            self.timestamp = None
        ptr=ptr+4
        (name_id_mapping_version, num_name_id_mappings) = struct.unpack(">BH", dec_o.unused_data[ptr:ptr+3])
        ptr=ptr+3
        start=ptr
        self.id_to_name = {}
        for i in range(0, num_name_id_mappings):
            (node_id, name_len) = struct.unpack(">HH", dec_o.unused_data[start:start+4])
            (name,) = struct.unpack(">{}s".format(name_len), dec_o.unused_data[start+4:start+4+name_len])
            self.id_to_name[node_id] = name.decode('utf8')
            start=start+4+name_len

    def only_air(self):
        """If there is only air in the dictionary, all Nodes are air as well"""
        return self.id_to_name == {0: 'air'} 

    def only_ignore(self):
        """ If there is only ignore in the dictionary, all Nodes are ignore as well"""
        return self.id_to_name == {0: 'ignore'} 

    def _coord_to_index(self,x,y,z):
        return x + y*16 + z*256

    def walk_nodes(self):
        for x in range(16):
            for z in range(16):
                for y in range(16):
                    i = self._coord_to_index(x,y,z)
                    if self.param0[i] in self.id_to_name:
                        yield Node(x,y,z,self,self.id_to_name[self.param0[i]])
                    else:
                        print("Not in dict")
                        yield Node(x,y,z,self,self.param0[i])
cache = {} 
class BlockManager():
    def __init__(self, path):
        full_path = os.path.join(path,"map.sqlite")
        if not os.path.exists(full_path):
            raise(Exception("File not found", path))
        self.conn = sqlite3.connect(full_path)

    def walk_blocks(self, complete_parse=False):
        for row in self.conn.execute("SELECT `pos`,`data` FROM `blocks`"):
            if complete_parse:
                b = Block(*row)
                b.parse_blob()
                yield b
            else:   
                yield Block(*row)

    def get_column(self, x, z, only_visible_blocks=True):
        """Get one column of Blocks. In-game this would be a tall tower 16x16 Nodes from below ground to top of sky."""
        psmin = coord2pos(x, -2048, z)
        psmax = coord2pos(x, 2047, z)
        for row in self.conn.execute("SELECT `pos`,`data` FROM `blocks` WHERE `pos`>=? AND `pos`<=? AND (`pos` - ?) % 4096 = 0", (psmin, psmax, psmin)):
            b = Block(*row)
            b.parse_blob()
            if b.only_air() and only_visible_blocks:
                continue
            if b.only_ignore() and only_visible_blocks:
                continue
            yield b   
            
    def find_ground(self, blocks):
        #Find the Node with with the highest y that is not air. The ground(tm)
            map = {}
            for b in blocks:
                for node in b.walk_nodes():
                    if node.content in ('ignore', 'air'):
                        continue
                    if node.absx not in map:
                        map[node.absx] = {node.absz:(node,)}
                    if node.absz not in map[node.absx]:
                        map[node.absx][node.absz] = (node,None)
                    if node.absy > map[node.absx][node.absz][0].absy: #only keep the top non air node
                        map[node.absx][node.absz] = (node, map[node.absx][node.absz][0]) 
            return map
    def make_tile(self, map, x, z):
        if not map:
            return(None)
        tile = Image.new('RGB', (256,256)) #This is the tile we serve to the browser
        for node_x in range(16):
            for node_y in range(16):
                try:
                    absx = x*16+node_x
                    absy = z*16+node_y
                    (node, node_below) = map[absx][absy]                        
                    im = Image.open(node.getTexture())
                    if node_below:
                        im_below = Image.open(node_below.getTexture())
                except(KeyError):
                    print("Error at {}, {}".format(absx, absy))
                    return(None)
                flipped_y = (16-node_y-1) #In the slippy map world everything is upside down
                if im.mode != 'RGBA':
                    im = im.convert('RGBA')
                if im_below != 'RGBA':
                    im_below = im_below.convert('RGBA')
                im_composite = Image.alpha_composite(im_below,im)
                tile.paste(im_composite,(node_x*16, flipped_y*16))
        return(tile)

class MainHandler(tornado.web.RequestHandler):
    def initialize(self, bm):
        self.bm = bm

    def get(self, zoom, x, y):
        x = int(x)
        y = int(y)*-1 
        self.set_header("Content-Type", "image/png")
        
#        if os.path.exists('images/3/{}/{}.png'.format(x,y)):
#            with open('images/3/{}/{}.png'.format(x,y), "rb") as f:
#                self.write(f.read())
#                return
        blocks = bm.get_column(x,y)
        ground_level = bm.find_ground(blocks)
        tile = bm.make_tile(ground_level, x, y)
        output = BytesIO()
        tile.save(output, "PNG")
        output.seek(0)            
        if not os.path.exists('images/3'):
            os.mkdir("images/3")
        if not os.path.exists('images/3/{}'.format(x)):
            os.mkdir("images/3/{}".format(x))
        with open("images/3/{}/{}.png".format(x,y*-1),'wb') as f:
            f.write(output.read())
        output.seek(0)
        self.write(output.read())
        output.close()
            

def make_tile_tree(bm):
    seen = {}
    for b in bm.walk_blocks():
        print('.',end="")
        sys.stdout.flush()
        if b.x in seen:
            if b.z in seen[b.x]:
                continue
            else:
                seen[b.x][b.z] = True
        else:
            seen[b.x] = {b.z:True}
        if os.path.exists("images/3/{}/{}.png".format(b.x,b.z*-1)):
            continue
        
        blocks = bm.get_column(b.x,b.z)
        ground_level = bm.find_ground(blocks)
        tile = bm.make_tile(ground_level, b.x, b.z)
        if not tile:
            continue
        output = BytesIO()
        tile.save(output, "PNG")
        output.seek(0)            
        if not os.path.exists("images/3"):
            os.mkdir("images/3")
        if not os.path.exists("images/3/{}".format(b.x)):
            os.mkdir("images/3/{}".format(b.x))    
        with open("images/3/{}/{}.png".format(b.x,b.z*-1),'wb') as f:
            f.write(output.read())
        output.close()
    print("Base level tiles done.")
    zoomlevels = 3
    wip_tiles = {}
    try:
        for x in range(min(seen), max(seen)):
            if not x in seen:
                continue
            for z in range(min(seen[x]), max(seen[x])):
                for zoom in range(zoomlevels):
                    factor = 1<<zoom+1
                    dst_size = int(256/factor)
                    zoom_prefix = zoomlevels-1-zoom
                    dst_tilex = int((x-x%factor)/factor)
                    dst_tilez = int((z-z%factor)/factor)
                    if not zoom_prefix in wip_tiles:
                        wip_tiles[zoom_prefix] = {}
                    if not dst_tilex in wip_tiles[zoom_prefix]:
                        wip_tiles[zoom_prefix][dst_tilex] = {}
                    if not dst_tilez in wip_tiles[zoom_prefix][dst_tilex]:
                        wip_tiles[zoom_prefix][dst_tilex][dst_tilez] = Image.new('RGB', (256,256))
                    try:
                        im = Image.open("images/3/{}/{}.png".format(x,z*-1)) #invert z which is browser y
                        im.thumbnail((dst_size,dst_size), Image.ANTIALIAS)
                    except(IOError) as e:
                        im = Image.new('RGB', (dst_size,dst_size))
                    wip_tiles[zoom_prefix][dst_tilex][dst_tilez].paste(im,(dst_size*(x%factor), dst_size*(factor-(z%factor)-1))) #Black magic to flip the order of the scaled tiles
    #                print(zoom_prefix,dst_tilex,dst_tilez, " Paste",x,z," at ",(dst_size*(x%factor), dst_size*(factor-(z%factor)-1)))
    except(KeyError):
        import pdb;pdb.set_trace()
    for zoom_prefix in wip_tiles:
        for x in wip_tiles[zoom_prefix]:
            for z in wip_tiles[zoom_prefix][x]:
                output = BytesIO()
                wip_tiles[zoom_prefix][x][z].save(output, "PNG")
                output.seek(0)
                if not os.path.exists('images/{}'.format(zoom_prefix)):
                    os.mkdir("images/{}".format(zoom_prefix))
                if not os.path.exists('images/{}/{}'.format(zoom_prefix,x)):
                    os.mkdir('images/{}/{}'.format(zoom_prefix, x))
                with open("images/{}/{}/{}.png".format(zoom_prefix,x,z*-1),'wb') as f:
                    f.write(output.read())
                print(".", end="")
                sys.stdout.flush()

class RestHandler(tornado.web.RequestHandler):
    def get(self, arg): 
        path = os.path.join(options.world, "players")
        players = {}
        for player in os.listdir(path):
            with open(os.path.join(path, player), 'r') as f:
                players[player] = {}
                for row in f.readlines():
                    if 'position' in row:
                        pos = row.split('=')[1]
                        (x,y,z) = pos.split(',')
                        x = x.replace('(','')
                        z= z.replace(')','')
                        x = float(x)*0.12 #magic constant
                        y = float(y)*0.12
                        z = float(z)*0.12
                        players[player]['position'] = (x,y,z)
        def to_GeoJSON(input):
            all = {"type": "FeatureCollection",
    "features": []}

            for player in input:
                out = {"type": "Feature",
                    "geometry": {
                        "type": "Point",
                        "coordinates": [input[player]['position'][0], input[player]['position'][2]]
                    },
                    "properties": {
                        "name": player
                    }
                }
            
                all["features"].append(out)
            return(all)


        def decimal_default(obj):
            if isinstance(obj, Decimal):
                return str(obj)
            raise TypeError
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps(to_GeoJSON(players), default=decimal_default))
if __name__ == "__main__":
    tornado.options.parse_command_line()
    bm = BlockManager(options.world)
    if options.generate:
        make_tile_tree(bm)
        sys.exit(0)
    application = tornado.web.Application([
    (r"/(-?[0-9]+)/(-?[0-9]+)/(-?[0-9]+)\.png", MainHandler, dict(bm=bm)),
    (r"/()", tornado.web.StaticFileHandler, dict(path=os.path.join(os.path.dirname(__file__),"index.html"))),
    (r"/(index2.html)", tornado.web.StaticFileHandler, dict(path=os.path.join(os.path.dirname(__file__)))),
    (r"/(.*\.js)", tornado.web.StaticFileHandler,     dict(path=os.path.join(os.path.dirname(__file__)))),
    (r"/(leaflet\.css)", tornado.web.StaticFileHandler,     dict(path=os.path.join(os.path.dirname(__file__)))),
    (r"/(slippy\.css)", tornado.web.StaticFileHandler,     dict(path=os.path.join(os.path.dirname(__file__)))),
    (r"/api/1.0/player/(.*)", RestHandler),
    (r"/images/(.*)", tornado.web.StaticFileHandler,     dict(path=os.path.join(os.path.dirname(__file__),'images'))) 
], debug=True)
    application.listen(60000)
    tornado.ioloop.IOLoop.instance().start()

#deps pillow